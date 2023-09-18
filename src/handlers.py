import logging
from typing import Any, Dict, List, Optional

import kopf
from kubernetes import client, config

from cache import Cache, MemoryCache
from kubernetes_utils import delete_secret, get_ns_list, sync_secret, patch_clustersecret_status, \
    create_secret_metadata, secret_exists, get_custom_objects_by_kind
from models import BaseClusterSecret

# In-memory dictionary for all ClusterSecrets in the Cluster. UID -> ClusterSecret Body
csecs_cache: Cache = MemoryCache()

from os_utils import in_cluster

# Loading kubeconfig
if in_cluster():
    # Loading kubeconfig
    config.load_incluster_config()
else:
    # Loading using the local kubevonfig.
    config.load_kube_config()

v1 = client.CoreV1Api()
custom_objects_api = client.CustomObjectsApi()


@kopf.on.delete('clustersecret.io', 'v1', 'clustersecrets')
def on_delete(
    body: Dict[str, Any],
    uid: str,
    name: str,
    logger: logging.Logger,
    **_,
):
    syncedns = body.get('status', {}).get('create_fn', {}).get('syncedns', [])
    for ns in syncedns:
        logger.info(f'deleting secret {name} from namespace {ns}')
        delete_secret(logger, ns, name, v1)

    # Delete from memory to prevent syncing with new namespaces
    try:
        csecs_cache.remove_cluster_secret(uid)
    except KeyError as k:
        logger.info(f'This csec were not found in memory, maybe it was created in another run: {k}')
        return
    logger.debug(f'csec {uid} deleted from memory ok')


@kopf.on.field('clustersecret.io', 'v1', 'clustersecrets', field='matchNamespace')
def on_field_match_namespace(
    old: Optional[List[str]],
    new: List[str],
    name: str,
    namespace: str,
    body,
    uid: str,
    logger: logging.Logger,
    **_,
):
    logger.debug(f'Namespaces changed: {old} -> {new}')

    if old is None:
        logger.debug('This is a new object: Ignoring.')
        return

    logger.debug(f'Updating Object body == {body}')

    syncedns = body.get('status', {}).get('create_fn', {}).get('syncedns', [])

    updated_matched = get_ns_list(logger, body, v1)
    to_add = set(updated_matched).difference(set(syncedns))
    to_remove = set(syncedns).difference(set(updated_matched))

    logger.debug(f'Add secret to namespaces: {to_add}, remove from: {to_remove}')

    for secret_namespace in to_add:
        sync_secret(logger, secret_namespace, body, v1)

    for secret_namespace in to_remove:
        delete_secret(logger, secret_namespace, name, v1=v1)

    cached_cluster_secret = csecs_cache.get_cluster_secret(uid)
    if cached_cluster_secret is None:
        logger.error('Received an event for an unknown ClusterSecret.')

    # Updating the cache
    csecs_cache.set_cluster_secret(BaseClusterSecret(
        uid=uid,
        name=name,
        namespace=namespace,
        data=body.get('data'),
        synced_namespace=updated_matched,
    ))

    # Patch synced_ns field
    logger.debug(f'Patching clustersecret {name} in namespace {namespace}')
    patch_clustersecret_status(
        logger=logger,
        namespace=namespace,
        name=name,
        new_status={'create_fn': {'syncedns': updated_matched}},
        custom_objects_api=custom_objects_api,
    )


@kopf.on.field('clustersecret.io', 'v1', 'clustersecrets', field='data')
def on_field_data(
    old: Dict[str, str],
    new: Dict[str, str],
    body: Dict[str, Any],
    meta: kopf.Meta,
    name: str,
    logger: logging.Logger,
    **_,
):
    logger.debug(f'Data changed: {old} -> {new}')
    if old is None:
        logger.debug('This is a new object: Ignoring')
        return

    logger.debug(f'Updating Object body == {body}')
    syncedns = body.get('status', {}).get('create_fn', {}).get('syncedns', [])

    secret_type = body.get('type', 'Opaque')

    for ns in syncedns:
        logger.info(f'Re Syncing secret {name} in ns {ns}')
        body = client.V1Secret(
            api_version='v1',
            data=new,
            kind='Secret',
            metadata=create_secret_metadata(
                name=name,
                namespace=ns,
                annotations=meta.annotations,
                labels=meta.labels,
            ),
            type=secret_type,
        )
        # Ensuring the secret still exist.
        if secret_exists(logger=logger, name=name, namespace=ns, v1=v1):
            response = v1.replace_namespaced_secret(name=name, namespace=ns, body=body)
        else:
            response = v1.create_namespaced_secret(namespace=ns, body=body)
        logger.debug(response)


@kopf.on.resume('clustersecret.io', 'v1', 'clustersecrets')
@kopf.on.create('clustersecret.io', 'v1', 'clustersecrets')
async def create_fn(
    logger: logging.Logger,
    uid: str,
    name: str,
    namespace: str,
    body: Dict[str, Any],
    **_
):
    # get all ns matching.
    matchedns = get_ns_list(logger, body, v1)

    # sync in all matched NS
    logger.info(f'Syncing on Namespaces: {matchedns}')
    for namespace in matchedns:
        sync_secret(logger, namespace, body, v1)

    # store status in memory
    cached_cluster_secret = csecs_cache.get_cluster_secret(uid)
    if cached_cluster_secret is None:
        logger.error('Received an event for an unknown ClusterSecret.')

    # Updating the cache
    csecs_cache.set_cluster_secret(BaseClusterSecret(
        uid=uid,
        name=name,
        namespace=namespace,
        data=body.get('data'),
        synced_namespace=matchedns,
    ))

    # return for what ??? Should be deleted ??
    return {'syncedns': matchedns}


@kopf.on.create('', 'v1', 'namespaces')
async def namespace_watcher(logger: logging.Logger, meta: kopf.Meta, **_):
    """Watch for namespace events
    """
    new_ns = meta.name
    logger.debug(f'New namespace created: {new_ns} re-syncing')
    ns_new_list = []
    for cluster_secret in csecs_cache.all_cluster_secret():
        obj_body = cluster_secret['body']
        name = obj_body['metadata']['name']

        matcheddns = cluster_secret['syncedns']

        logger.debug(f'Old matched namespace: {matcheddns} - name: {name}')
        ns_new_list = get_ns_list(logger, obj_body, v1)
        logger.debug(f'new matched list: {ns_new_list}')
        if new_ns in ns_new_list:
            logger.debug(f'Cloning secret {name} into the new namespace {new_ns}')
            sync_secret(
                logger=logger,
                namespace=new_ns,
                body=obj_body,
                v1=v1,
            )

            # if there is a new matching ns, refresh cache
            cluster_secret.namespace = ns_new_list
            csecs_cache.set_cluster_secret(cluster_secret)

    # update ns_new_list on the object so then we also delete from there
    return {'syncedns': ns_new_list}


@kopf.on.startup()
async def startup_fn(logger: logging.Logger, **_):
    logger.debug(
        """
      #########################################################################
      # DEBUG MODE ON - NOT FOR PRODUCTION                                    #
      # On this mode secrets are leaked to stdout, this is not safe!. NO-GO ! #
      #########################################################################
    """,
    )

    cluster_secrets = get_custom_objects_by_kind(
        group='clustersecret.io',
        version='v1',
        plural='clustersecrets',
        custom_objects_api=custom_objects_api,
    )

    logger.info(f'Found {len(cluster_secrets)} existing cluster secrets.')
    for item in cluster_secrets:
        metadata = item.get('metadata')
        csecs_cache.set_cluster_secret(
            BaseClusterSecret(
                uid=metadata.get('uid'),
                name=metadata.get('name'),
                namespace=metadata.get('namespace'),
                data=item.get('data'),
            )
        )

import logging
from time import sleep

from kubernetes import client

from .memcached_tpr_v1alpha1_api import MemcachedThirdPartyResourceV1Alpha1Api
from .kubernetes_resources import (get_default_label_selector,
                                   get_mcrouter_service_object,
                                   get_memcached_service_object)
from .kubernetes_helpers import (create_service,
                                 update_service,
                                 delete_service,
                                 create_config_map,
                                 update_config_map,
                                 delete_config_map,
                                 create_memcached_deployment,
                                 create_mcrouter_deployment,
                                 update_memcached_deployment,
                                 update_mcrouter_deployment,
                                 reap_deployment)


def periodical_check(shutting_down, sleep_seconds):
    logging.info('thread started')
    while not shutting_down.isSet():
        try:
            # First make sure all expected resources exist
            check_existing()

            # Then garbage collect resources from deleted clusters
            collect_garbage()
        except Exception as e:
            # Last resort: catch all exceptions to keep the thread alive
            logging.exception(e)
        finally:
            sleep(int(sleep_seconds))
    else:
        logging.info('thread stopped')


VERSION_CACHE = {}


def is_version_cached(resource):
    uid = resource.metadata.uid
    version = resource.metadata.resource_version

    if uid in VERSION_CACHE and VERSION_CACHE[uid] == version:
        return True

    return False


def cache_version(resource):
    uid = resource.metadata.uid
    version = resource.metadata.resource_version

    VERSION_CACHE[uid] = version


def check_existing():
    memcached_tpr_api = MemcachedThirdPartyResourceV1Alpha1Api()
    try:
        cluster_list = memcached_tpr_api.list_memcached_for_all_namespaces()
    except client.rest.ApiException as e:
        # If for any reason, k8s api gives us an error here, there is
        # nothing for us to do but retry later
        logging.exception(e)
        return False

    v1 = client.CoreV1Api()
    v1beta1api = client.ExtensionsV1beta1Api()
    for cluster_object in cluster_list['items']:
        name = cluster_object['metadata']['name']
        namespace = cluster_object['metadata']['namespace']

        service_objects = [
            get_mcrouter_service_object(cluster_object),
            get_memcached_service_object(cluster_object)]
        for service_object in service_objects:
            # Check service exists
            service_name = service_object.metadata.name
            try:
                service = v1.read_namespaced_service(service_name, namespace)
            except client.rest.ApiException as e:
                if e.status == 404:
                    # Create missing service
                    created_service = create_service(service_object)
                    if created_service:
                        # Store latest version in cache
                        cache_version(created_service)
                else:
                    logging.exception(e)
            else:
                if not is_version_cached(service):
                    # Update since we don't know if it's configured correctly
                    updated_service = update_service(service_object)
                    if updated_service:
                        # Store latest version in cache
                        cache_version(updated_service)

        # Check config map exists
        try:
            config_map = v1.read_namespaced_config_map(name, namespace)
        except client.rest.ApiException as e:
            if e.status == 404:
                # Create missing service
                create_config_map(cluster_object)
            else:
                logging.exception(e)
        else:
            update_config_map(cluster_object)

        # Check memcached deployment exists
        try:
            deployment = v1beta1api.read_namespaced_deployment(name, namespace)
        except client.rest.ApiException as e:
            if e.status == 404:
                # Create missing deployment
                created_memcached_deployment = create_memcached_deployment(
                    cluster_object)
                if created_memcached_deployment:
                    # Store latest version in cache
                    cache_version(created_memcached_deployment)
            else:
                logging.exception(e)
        else:
            if not is_version_cached(deployment):
                # Update since we don't know if it's configured correctly
                updated_memcached_deployment = update_memcached_deployment(cluster_object)
                if updated_memcached_deployment:
                    # Store latest version in cache
                    cache_version(updated_memcached_deployment)

        # Check mcrouter deployment exists
        try:
            deployment = v1beta1api.read_namespaced_deployment(
                '{}-router'.format(name),
                namespace)
        except client.rest.ApiException as e:
            if e.status == 404:
                # Create missing deployment
                created_mcrouter_deployment = create_mcrouter_deployment(
                    cluster_object)
                if created_mcrouter_deployment:
                    # Store latest version in cache
                    cache_version(created_mcrouter_deployment)
            else:
                logging.exception(e)
        else:
            if not is_version_cached(deployment):
                # Update since we don't know if it's configured correctly
                updated_mcrouter_deployment = update_mcrouter_deployment(cluster_object)
                if updated_mcrouter_deployment:
                    # Store latest version in cache
                    cache_version(updated_mcrouter_deployment)


def collect_garbage():
    memcached_tpr_api = MemcachedThirdPartyResourceV1Alpha1Api()
    v1 = client.CoreV1Api()
    v1beta1api = client.ExtensionsV1beta1Api()
    label_selector = get_default_label_selector()

    # Find all services that match our labels
    try:
        service_list = v1.list_service_for_all_namespaces(
            label_selector=label_selector)
    except client.rest.ApiException as e:
        logging.exception(e)
    else:
        # Check if service belongs to an existing cluster
        for service in service_list.items:
            cluster_name = service.metadata.labels['cluster']
            name = service.metadata.name
            namespace = service.metadata.namespace

            try:
                memcached_tpr_api.read_namespaced_memcached(
                    cluster_name, namespace)
            except client.rest.ApiException as e:
                if e.status == 404:
                    # Delete service
                    delete_service(name, namespace)
                else:
                    logging.exception(e)

    # Find all config maps that match our labels
    try:
        config_map_list = v1.list_config_map_for_all_namespaces(
            label_selector=label_selector)
    except client.rest.ApiException as e:
        logging.exception(e)
    else:
        # Check if service belongs to an existing cluster
        for config_map in config_map_list.items:
            cluster_name = config_map.metadata.labels['cluster']
            name = config_map.metadata.name
            namespace = config_map.metadata.namespace

            try:
                memcached_tpr_api.read_namespaced_memcached(
                    cluster_name, namespace)
            except client.rest.ApiException as e:
                if e.status == 404:
                    # Delete config map
                    delete_config_map(name, namespace)
                else:
                    logging.exception(e)

    # Find all deployments that match our labels
    try:
        deployment_list = v1beta1api.list_deployment_for_all_namespaces(
            label_selector=label_selector)
    except client.rest.ApiException as e:
        logging.exception(e)
    else:
        # Check if deployment belongs to an existing cluster
        for deployment in deployment_list.items:
            cluster_name = deployment.metadata.labels['cluster']
            name = deployment.metadata.name
            namespace = deployment.metadata.namespace

            try:
                memcached_tpr_api.read_namespaced_memcached(
                    cluster_name, namespace)
            except client.rest.ApiException as e:
                if e.status == 404:
                    # Gracefully delete deployment, replicaset and pods
                    reap_deployment(name, namespace)
                else:
                    logging.exception(e)

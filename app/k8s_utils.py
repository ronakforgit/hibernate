import logging
from utils import basic_retry
from kubernetes.client.rest import ApiException

class K8sAPIError(Exception):
    pass


@basic_retry(attempts=3, pause=10)
def cordon_all_nodes(client, protect_removal_disabled: str, exclude_node_id: str):
    """cordon all the nodes so essential components could be scheduled only on hybernation node"""
    logging.info("Cordon function")

    node_body = {
        "spec": {
            "unschedulable": True
        }
    }

    node_list = client.list_node()
    for node in node_list.items:
        logging.info("Inspecting node %s to cordon", node.metadata.name)
        if node.metadata.labels.get("autoscaling.cast.ai/removal-disabled") == "true" and protect_removal_disabled == "true":
            logging.info("skip Cordoning node: %s due protect_removal_disabled label" % node.metadata.name)
            continue
        if node.metadata.labels.get("provisioner.cast.ai/node-id") == exclude_node_id:
            logging.info("skip Cordoning hibernation node: %s" % node.metadata.name)
            continue
        logging.info("Cordoning: %s" % node.metadata.name)
        client.patch_node(node.metadata.name, node_body)


def deployment_tolerates(deployment, toleration):
    """" check if deployment tolerates a taint on a hibernation node"""
    if deployment.spec.template.spec.tolerations:
        if [tol_key for tol_key in deployment.spec.template.spec.tolerations if tol_key.key == toleration]:
            return True
    return False


@basic_retry(attempts=3, pause=15)
def add_special_toleration(client, deployment: str, toleration: str):
    """" modify essential deployment to keep them running on hibernation node (tolerate node)"""
    logging.info("add tolerations to essential workloads function")

    toleration_to_add = {
        'key': toleration,
        'effect': 'NoSchedule',
        'operator': 'Exists'
    }

    deployment_name = deployment.metadata.name
    if not deployment_tolerates(deployment, toleration):
        current_tolerations = deployment.spec.template.spec.tolerations
        logging.info("Patching and restarting: %s" % deployment_name)
        if current_tolerations is None:
            current_tolerations = []
        current_tolerations.append(toleration_to_add)

        restart_body = {
            'spec': {
                'template': {
                    'spec': {
                        'tolerations': current_tolerations
                    }
                }
            }
        }

        patch_result = None
        try:
            patch_result = client.patch_namespaced_deployment(deployment_name, deployment.metadata.namespace,
                                                              restart_body)
        except ApiException as e:
            raise K8sAPIError(
                f'Exception when calling patching deployment: {deployment_name} with result {patch_result}') from e

        if patch_result:
            logging.info("Patch complete...")
    else:
        logging.info(f'SKIP Deployment {deployment_name} already has toleration')


def check_hibernation_node_readiness(client, taint: str, node_name: str):
    """ check if node has taint """
    node = client.read_node(node_name)
    if check_if_node_has_specific_taint(client, taint, node_name):
        if node_is_ready(node):
            logging.info("found tainted hibernation node %s", node_name)
            return node.metadata.labels.get("provisioner.cast.ai/node-id")
    logging.info("Hibernation node %s is not READY %s", node_name)
    return None


@basic_retry(attempts=3, pause=10)
def node_is_ready(node: str):
    """ check if node is ready """
    node_scheduling = node.spec.unschedulable
    for condition in node.status.conditions:
        if condition.status and condition.type == "Ready" and not node_scheduling:
            return True
    return False

def check_if_node_has_specific_taint(client, taint: str, node_name: str):
    """ check if node has taint """
    node = client.read_node(node_name)

    if node.spec.taints:
        for current_taint in node.spec.taints:
            if current_taint.to_dict()["key"] == taint:
                logging.info("found tainted node %s", node_name)
                return True
    logging.info("Node %s is not tainted", node_name)
    return False

@basic_retry(attempts=2, pause=5)
def add_node_taint(client, pause_taint: str, node_name):
    """ add specific taint to node"""
    logging.info(f'patching node {node_name} to add {pause_taint} taint')

    k8s_label = "kubernetes.io/hostname=" + node_name
    node = client.list_node(label_selector=k8s_label).items[0]

    taint_to_add = {"key": pause_taint, "effect": "NoSchedule"}
    current_taints = node.spec.taints
    if current_taints is None:
        current_taints = []
    elif check_if_node_has_specific_taint(client, pause_taint, node_name):
        logging.info(f'node {node_name} already has {pause_taint} taint')
        return

    current_taints.append(taint_to_add)

    taint_body = {
        "spec": {
            "taints": current_taints
        },
        "metadata": {
            "labels": {
                "scheduling.cast.ai/paused-cluster": "true",
                "scheduling.cast.ai/spot-fallback": "true",
                "scheduling.cast.ai/spot": "true"
            }
        }
    }

    try:
        patch_result = client.patch_node(node.metadata.name, taint_body)
    except ApiException as e:
        raise K8sAPIError(f'Failed to taint node {node.metadata.name}') from e

    if patch_result:
        logging.info("node %s successfully patched", node.metadata.name)


@basic_retry(attempts=3, pause=5)
def remove_node_taint(client, pause_taint: str, node_id: str):
    """ remove specific taint from node"""
    node = client.list_node(label_selector=f"provisioner.cast.ai/node-id={node_id}").items[0]
    node_name = node.metadata.name

    logging.info(f'patching node {node_name} to remove {pause_taint} taint')

    taints = node.spec.taints
    filtered_taints = list(filter(lambda x: x.key != pause_taint, taints))
    taint_body = {"spec": {"taints": filtered_taints}}

    try:
        patch_result = client.patch_node(node.metadata.name, taint_body)
    except ApiException as e:
        raise K8sAPIError(f'Failed to remove taint from node {node.metadata.name}') from e

    if patch_result:
        logging.info(f'node {node_name} successfully patched')
        return True
    else:
        logging.error(f'failed to patch node {node_name}, with details {patch_result}')


@basic_retry(attempts=3, pause=15)
def get_node_castai_id(client, node_name: str):
    """" Node with hibernation taint already exist """
    k8s_label = "kubernetes.io/hostname=" + node_name
    node_list = client.list_node(label_selector=k8s_label)
    if len(node_list.items) == 1:
        for node in node_list.items:
            node_id = node.metadata.labels.get("provisioner.cast.ai/node-id")
            logging.info("found Node %s with id %s that is running Pause Job ", node.metadata.name, node_id)
            return node_id


@basic_retry(attempts=3, pause=15)
def get_deployments_names_with_system_priority_class(client):
    """Return all system-critical priority-class deployments"""
    deployments = client.list_deployment_for_all_namespaces()
    critical_deploys = []
    for deployment in deployments.items:
        if has_system_priority_class(deployment): critical_deploys.append(deployment)
    return critical_deploys


@basic_retry(attempts=3, pause=15)
def has_system_priority_class(deployment):
    """ validate if Deployment is system critical"""
    if deployment.spec.template.spec.priority_class_name in ("system-cluster-critical", "system-node-critical"):
        logging.info(f'SYSTEM CRITICAL {deployment.metadata.name} found')
        return True
    else:
        return False

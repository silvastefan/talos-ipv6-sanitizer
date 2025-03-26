from kubernetes import client, config, watch
import time

def sanitize_node_annotations():
    config.load_incluster_config()
    v1 = client.CoreV1Api()
    w = watch.Watch()

    for event in w.stream(v1.list_node):
        node = event['object']
        annotations = node.metadata.annotations or {}
        key = "alpha.kubernetes.io/provided-node-ip"
        if key in annotations and "," in annotations[key]:
            ipv4 = annotations[key].split(",")[0]
            print(f"Sanitizing node {node.metadata.name}, keeping IPv4: {ipv4}")
            body = {"metadata": {"annotations": {key: ipv4}}}
            v1.patch_node(node.metadata.name, body)

if __name__ == "__main__":
    while True:
        try:
            sanitize_node_annotations()
        except Exception as e:
            print(f"Erro: {e}")
        time.sleep(30)

from kubernetes import client, config, watch
from urllib3.exceptions import ReadTimeoutError
import time

def is_ipv4(address: str) -> bool:
    """
    Verifica se `address` é um IPv4.
    """
    parts = address.strip().split('.')
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit():
            return False
        if not 0 <= int(part) <= 255:
            return False
    return True

def remove_ipv6_from_annotation(annotation_value: str) -> str:
    """
    Recebe algo como "10.0.0.7,2a01:4f9:c013:1a15::1"
    e retorna apenas os endereços IPv4, ex: "10.0.0.7".
    Se houver vários IPv4, mantém todos, separados por vírgula.
    """
    addresses = [addr.strip() for addr in annotation_value.split(',')]
    ipv4_addrs = [addr for addr in addresses if is_ipv4(addr)]
    return ','.join(ipv4_addrs)

def main():
    """
    Observa (Watch) a criação (e modificação) de Nodes no cluster.
    Quando um Node é detectado, se ele tiver a anotação
    alpha.kubernetes.io/provided-node-ip, remove qualquer IPv6
    e atualiza a anotação mantendo apenas IPv4.
    """
    try:
        # Tenta carregar configuração de dentro do cluster (quando rodar dentro de um Pod)
        config.load_incluster_config()
    except:
        # Caso esteja executando fora do cluster (ex: local)
        config.load_kube_config()

    v1 = client.CoreV1Api()
    w = watch.Watch()

    print("Iniciando controller para remover IPv6 das anotações de Node...")

    while True:
        try:
            for event in w.stream(v1.list_node, _request_timeout=60):
                # Verifica se o tipo de evento é 'ADDED' ou 'MODIFIED'
                if event['type'] in ['ADDED', 'MODIFIED']:
                    node = event['object']
                    annotations = node.metadata.annotations or {}
                    ip_annotation_key = 'alpha.kubernetes.io/provided-node-ip'

                    if ip_annotation_key in annotations:
                        original_value = annotations[ip_annotation_key]
                        new_value = remove_ipv6_from_annotation(original_value)
                        
                        if new_value != original_value:
                            print(f"[INFO] Node {node.metadata.name}: alterando anotação de '{original_value}' para '{new_value}'")
                            
                            body = {
                                "metadata": {
                                    "annotations": {
                                        ip_annotation_key: new_value
                                    }
                                }
                            }
                            try:
                                v1.patch_node(node.metadata.name, body)
                                print(f"[INFO] Node {node.metadata.name} atualizado com sucesso.")
                            except Exception as e:
                                print(f"[ERRO] Falha ao atualizar Node {node.metadata.name}: {e}")
        except ReadTimeoutError:
            print("[WARN] Timeout na conexão Watch. Reiniciando watch...")
            time.sleep(1)
            continue  # Reinicia o while True
        except Exception as e:
            # Se quiser tratar outros erros, faça aqui
            print(f"[ERRO] Ocorreu um erro inesperado: {e}")
            time.sleep(5)
            continue
        
if __name__ == "__main__":
    main()
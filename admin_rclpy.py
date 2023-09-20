try:
    import rclpy
    from rclpy.node import Node
except ImportError as exc:
    raise ImportError("Problema de import") from exc


def inicializacao_rclpy() -> None:
    """
    Inicializa a biblioteca rclpy

    :return: Nada
    """

    rclpy.init(args=None)


def gera_spin(node_para_spin: Node) -> None:
    """
    Gera um spin no nó do parâmetro

    :param node_para_spin: Nó para spin
    :return: Noda
    """

    while rclpy.ok():
        rclpy.spin_once(node_para_spin)


def destroi_node(node_para_finalizar: Node) -> None:
    """
    Destrói o nó que está sendo passado por parâmetro

    :param node_para_finalizar: Nó para destruir
    :return: Nada
    """

    node_para_finalizar.destroy_node()


def finaliza_rclpy() -> None:
    """
    Finaliza biblioteca rclpy

    :return: Nada
    """

    rclpy.shutdown()
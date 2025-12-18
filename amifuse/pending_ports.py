"""
Simple pending message registry to inject messages into ports when WaitPort is called.
"""
pending_msgs = {}
default_msgs = []
last_wait_port = None

def queue_msg(port_addr, msg_addr):
    pending_msgs.setdefault(port_addr, []).append(msg_addr)

def pop_msg(port_addr):
    lst = pending_msgs.get(port_addr)
    if not lst:
        return None
    return lst.pop(0)

def queue_default(msg_addr):
    default_msgs.append(msg_addr)

def pop_default():
    if not default_msgs:
        return None
    return default_msgs.pop(0)

def set_last_wait_port(port_addr):
    global last_wait_port
    last_wait_port = port_addr

def get_last_wait_port():
    return last_wait_port

import socket
import threading
import re
from datetime import datetime, timedelta
import time
import argparse

redis_data = {}
server_data = {}
BUFFER_SIZE = 4096

def encode_bulk_string(s: str) -> bytes:
    return ("$"+ str(len(s)) + "\r\n" + s + "\r\n").encode()

def encode_error_message(err: str) -> bytes:
    return ("-ERR " + err+ "\r\n").encode()
    
def expiration_cleanup(redis_data: dict):
    while True:
        # Iterate over keys and remove expired keys
        for key in list(redis_data.keys()):
            if redis_data[key].get('expiry') and redis_data[key]['expiry'] < datetime.now():
                del redis_data[key]
        # Sleep for some time before checking again (e.g., every minute)
        time.sleep(60)

def send_server_info(client, replica):
    result = "role:"+("master" if not replica else "slave") + "\r\n"
    result = result + "master_replid:8371b4fb1155b71f4a04d3e1bc3e18c4a990aeeb" + "\r\n"
    result = result + "master_repl_offset:0" + "\r\n"
    client.send(encode_bulk_string(result)) 

def handle_client(client, redis_data: dict, replica=None):
    def split_segments(s):
        segments = s.split('$')
        if segments[0].startswith('*'):
            segments[0] = segments[0][1:]
        segments = [seg for seg in segments if seg]
        segments = [seg if not seg.endswith('$') else seg for seg in segments]
        processed_segments = []
        for seg in segments:
            processed_segment = re.sub(r'^\d+\r\n', '', seg)
            processed_segment = processed_segment.rstrip('\r\n')
            processed_segments.append(processed_segment)
        return processed_segments   

    while client:
        req = client.recv(BUFFER_SIZE)
        data: str = req.decode() 
        cmds_list = split_segments(data)
        cmds = iter(cmds_list)
        if not cmds_list:
            break
        while True:
            if not cmds_list:
                break
            try:
                cmd = next(cmds)
            except StopIteration:
                break
            if cmd == '':
                continue
            if cmd.lower() == 'set':
                key = None
                value = None
                try:
                    key = next(cmds)
                    value = next(cmds)
                    expiry_cmd = next(cmds)
                    expiry = None
                    if expiry_cmd.lower() == 'px': 
                        ms = int(next(cmds))
                        expiry = datetime.now() + timedelta(milliseconds=ms)
                    redis_data[key] = {"value": value, "expiry": expiry}
                    client.send(b"+OK\r\n")  
                except StopIteration:
                    # if there is no expiration added, just send the value
                    if key and value:
                        redis_data[key] = {"value": value}
                        client.send(b"+OK\r\n")
                    else:
                        client.send(b"$-1\r\n")  
                    break
            if cmd.lower() == 'get':
                try:
                    key = next(cmds)
                    get_data = redis_data.get(key)
                    if get_data:
                        value = get_data['value']
                        expiry = get_data.get('expiry')
                        if expiry and expiry < datetime.now():
                            del redis_data[key]  # Remove expired key
                            client.send(b"$-1\r\n")
                        else:
                            client.send(encode_bulk_string(value))
                    else:
                        client.send(b"$-1\r\n")
                except StopIteration:
                    client.send(b"$-1\r\n")
                    break
            if cmd.lower() == 'ping':
                client.send(b"+PONG\r\n")
                break
            if cmd.lower() == 'echo':
                try:
                    content = "+" + next(cmds) + "\r\n"
                    client.send(content.encode())
                    break
                except StopIteration:
                    err = encode_error_message("echo msg not given")
                    client.send(err)
                    break
            if cmd.lower() == 'info':
                try:
                    repl = next(cmds)
                    send_server_info(client, replica)
                except StopIteration:
                    break
            if cmd.lower() == 'replconf':
                try:
                    client.send(b"+OK\r\n")
                except StopIteration:
                    break   

def connect_to_master(host, port, replica_port):
    with socket.create_connection(("localhost", port)) as s:
        s.send(b"*1\r\n$4\r\nPING\r\n")
        res = s.recv(BUFFER_SIZE)
        print(res)
        repl_conf_str = f"*3\r\n$8\r\nREPLCONF\r\n$14\r\nlistening-port\r\n$4\r\n{replica_port}\r\n"
        s.send(repl_conf_str.encode())
        res = s.recv(BUFFER_SIZE)
        print(res)
        repl_conf_str = "*3\r\n$8\r\nREPLCONF\r\n$4\r\ncapa\r\n$6\r\npsync2\r\n"
        s.send(repl_conf_str.encode())
        res = s.recv(BUFFER_SIZE)
        print(res)

def main(port=6379, replica=None, from_replica=False):
    # Start expiration cleanup thread
    cleanup_thread = threading.Thread(target=expiration_cleanup, args=(redis_data,))
    cleanup_thread.daemon = True
    cleanup_thread.start()
        

    print(f"Logs from your program will appear here in port {port}!")
    server_socket = socket.create_server(("localhost", port), reuse_port=True)
 
    while True:
        client, _ = server_socket.accept() # wait for client
        server_data[port] = server_socket
        thread = threading.Thread(target=handle_client, args=(client, redis_data, replica))
        thread.start()

if __name__ == "__main__":
    try:
        argsParser = argparse.ArgumentParser("A Redis server written in Python")
        argsParser.add_argument("--port", dest="port", default=6379)
        argsParser.add_argument("--replicaof", type=str, dest="replica")
        args = argsParser.parse_args()
        port = int(args.port)
        
        replica = args.replica
        if replica:
            master_host, master_port = replica.split(" ")[0], replica.split(" ")[1]
            master_port = int(master_port)
            master_server = connect_to_master(master_host, master_port, port)
            server_data["master"] = master_server
            main(port, replica)
        else:
            main(port, None)
    finally:
        print("CLOSE ALL CONNECTIONS")
        for repl in server_data:
            server_data[repl].close()



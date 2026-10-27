import argparse
from pathlib import Path
from .server import make_server

def main():
    parser = argparse.ArgumentParser(description='Merchant Payment Control Center — local sandbox')
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--db', default='data/payments.sqlite3')
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    server = make_server(args.db, args.port)
    print(f'Merchant Payment Control Center: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
if __name__ == '__main__':
    main()

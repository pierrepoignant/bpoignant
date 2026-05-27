import argparse
from __init__ import create_app

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-p', '--port', help='Port', type=int, default=5007)
    parser.add_argument('--debug', help='Enable debug mode', action='store_true')
    args = parser.parse_args()

    app = create_app()
    app.run(host='0.0.0.0', port=args.port, debug=args.debug)

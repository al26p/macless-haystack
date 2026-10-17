#!/usr/bin/env python3

import base64
import json
import logging
import os
import ssl
import sys
from collections import OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

import mac_auth
import mh_config

logger = logging.getLogger()


class ServerHandler(BaseHTTPRequestHandler):

    def addCORSHeaders(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header("Access-Control-Allow-Headers", "X-Requested-With")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Headers", "Authorization")
        self.send_header("Access-Control-Allow-Private-Network","true")

    def sendJson(self, status, payload):
        self.send_response(status)
        self.addCORSHeaders()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def authenticate(self):
        endpoint_user = mh_config.getEndpointUser()
        endpoint_pass = mh_config.getEndpointPass()
        if (endpoint_user is None or endpoint_user == "") and (endpoint_pass is None or endpoint_pass == ""):
            return True

        auth_header = self.headers.get('authorization')
        if auth_header:
            auth_type, auth_encoded = auth_header.split(None, 1)
            if auth_type.lower() == 'basic':
                auth_decoded = base64.b64decode(auth_encoded).decode('utf-8')
                username, password = auth_decoded.split(':', 1)
                if username == endpoint_user and password == endpoint_pass:
                    return True

        return False

    def do_OPTIONS(self):
        self.send_response(200, "ok")
        self.addCORSHeaders()
        self.end_headers()

    def do_GET(self):
        if not self.authenticate():
            self.send_response(401)
            self.addCORSHeaders()
            self.send_header('WWW-Authenticate', 'Basic realm="Auth Realm"')
            self.end_headers()
            return
        path = self.path.split('?', 1)[0].rstrip('/')
        if path in ('/status', '/login/status'):
            self.sendJson(200, mac_auth.status())
            return
        self.send_response(200)
        self.addCORSHeaders()
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Nothing to see here")

    def do_POST(self):
        if not self.authenticate():
            self.send_response(401)
            self.addCORSHeaders()
            self.send_header('WWW-Authenticate', 'Basic realm="Auth Realm"')
            self.end_headers()
            return
        if hasattr(self.headers, 'getheader'):
            content_len = int(self.headers.getheader('content-length', 0))
        else:
            content_len = int(self.headers.get('content-length') or 0)

        post_body = self.rfile.read(content_len)

        path = self.path.split('?', 1)[0].rstrip('/')
        if path == '/login':
            result = mac_auth.start_login()
            self.sendJson(202 if result.get("ok") else 409, result)
            return
        if path in ('/2fa', '/login/2fa'):
            self.handle2fa(post_body)
            return

        logger.debug('Getting with post: ' + str(post_body))
        body = json.loads(post_body)
        if "days" in body:
            days = body['days']
        else:
            days = 7
        logger.debug('Querying for ' + str(days) + ' days')
        unixEpoch = int(datetime.now().strftime('%s'))
        startdate = unixEpoch - (60 * 60 * 24 * days)

        datetime.fromtimestamp(startdate)

        # Date is always 1, because it has no effect
        data = {"search": [
            {"startDate": 1, "ids": list(body['ids'])}]}

        try:
            r = mac_auth.fetch(data)
            r.raise_for_status()

            logger.debug('Return from fetch service:')
            logger.debug(r.content.decode())
            result = json.loads(r.content.decode())
            results = result['results']

            newResults = OrderedDict()

            for idx, entry in enumerate(results):
                data = base64.b64decode(entry['payload'])
                timestamp = int.from_bytes(data[0:4], 'big') + 978307200
                if (timestamp > startdate):
                    newResults[timestamp] = entry

            sorted_map = OrderedDict(sorted(newResults.items(), reverse=True))

            result["results"] = list(sorted_map.values())
            self.send_response(200)
            # send response headers
            self.addCORSHeaders()
            self.end_headers()

            # send the body of the response
            responseBody = json.dumps(result)
            self.wfile.write(responseBody.encode())
        except mac_auth.ReloginRequired as e:
            logger.error("Apple requires a new login: %s", e)
            self.sendJson(503, {"error": "relogin_required",
                                "message": "POST /login to authenticate, then POST the code to /2fa"})
        except requests.exceptions.ConnectTimeout:
            logger.error("Timeout to fetch service, is your network reachable?")
            self.sendJson(504, {"error": "timeout"})
        except Exception as e:
            logger.error(f"Unknown error occurred {e}", exc_info=True)
            self.sendJson(501, {"error": "internal_error"})

    def handle2fa(self, post_body):
        try:
            code = json.loads(post_body.decode() or "{}").get("code", "")
        except (ValueError, UnicodeDecodeError):
            code = ""
        if not code:
            self.sendJson(400, {"ok": False, "error": "missing code"})
            return
        result = mac_auth.submit_2fa(code)
        self.sendJson(200 if result.get("ok") else 409, result)


class Server(HTTPServer):
    """HTTPServer that treats a client hanging up as a non-event."""

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            logger.debug("client %s disconnected: %s", client_address, error)
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    logging.info('Using os-findmy-e emulated macOS anisette for auth')
    logging.info('Token file: ' + mh_config.getConfigFile())
    if mac_auth.recover_session():
        logging.info('Recovered the Apple session from session.json')
    if not mac_auth.has_session():
        if sys.stdin.isatty():
            logging.info('No Apple session; starting an interactive login.')
            try:
                mac_auth.login()
            except Exception as e:
                logger.error(f"Apple login failed: {e}")
        else:
            logging.warning('No Apple session yet. Start one with POST /login, '
                            'then POST the 2FA code to /2fa. No automatic login is attempted.')

    Handler = ServerHandler

    httpd = Server((mh_config.getBindingAddress(), mh_config.getPort()), Handler)
    httpd.timeout = 30
    address = mh_config.getBindingAddress() + ":" + str(mh_config.getPort())
    if os.path.isfile(mh_config.getCertFile()):
        logger.info("Certificate file " + mh_config.getCertFile() +
                    " exists, so using SSL")
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=mh_config.getCertFile(
        ), keyfile=mh_config.getKeyFile() if os.path.isfile(mh_config.getKeyFile()) else None)

        httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)

        logger.info("serving at " + address + " over HTTPS")
    else:
        logger.info("Certificate file " + mh_config.getCertFile() +
                    " not found, so not using SSL")
        logger.info("serving at " + address + " over HTTP")
    user = mh_config.getEndpointUser()
    passw = mh_config.getEndpointPass()
    if (user is None or user == "") and (passw is None or passw == ""):
        logger.warning("Endpoint is not protected by authentication")
    else:
        logger.info("Endpoint is protected by authentication")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        logger.info('Server stopped')

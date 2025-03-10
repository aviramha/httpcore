from http import HTTPStatus
from ssl import SSLContext
from typing import Tuple, cast

from .._exceptions import ProxyError
from .._types import URL, Headers, TimeoutDict
from .._utils import get_logger, url_to_origin
from .base import SyncByteStream
from .connection import SyncHTTPConnection
from .connection_pool import SyncConnectionPool, ResponseByteStream

logger = get_logger(__name__)


def get_reason_phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return ""


def merge_headers(
    default_headers: Headers = None, override_headers: Headers = None
) -> Headers:
    """
    Append default_headers and override_headers, de-duplicating if a key existing in
    both cases.
    """
    default_headers = [] if default_headers is None else default_headers
    override_headers = [] if override_headers is None else override_headers
    has_override = set([key.lower() for key, value in override_headers])
    default_headers = [
        (key, value)
        for key, value in default_headers
        if key.lower() not in has_override
    ]
    return default_headers + override_headers


class SyncHTTPProxy(SyncConnectionPool):
    """
    A connection pool for making HTTP requests via an HTTP proxy.

    Parameters
    ----------
    proxy_url:
        The URL of the proxy service as a 4-tuple of (scheme, host, port, path).
    proxy_headers:
        A list of proxy headers to include.
    proxy_mode:
        A proxy mode to operate in. May be "DEFAULT", "FORWARD_ONLY", or "TUNNEL_ONLY".
    ssl_context:
        An SSL context to use for verifying connections.
    max_connections:
        The maximum number of concurrent connections to allow.
    max_keepalive_connections:
        The maximum number of connections to allow before closing keep-alive
        connections.
    http2:
        Enable HTTP/2 support.
    """

    def __init__(
        self,
        proxy_url: URL,
        proxy_headers: Headers = None,
        proxy_mode: str = "DEFAULT",
        ssl_context: SSLContext = None,
        max_connections: int = None,
        max_keepalive_connections: int = None,
        keepalive_expiry: float = None,
        http2: bool = False,
        backend: str = "sync",
        # Deprecated argument style:
        max_keepalive: int = None,
    ):
        assert proxy_mode in ("DEFAULT", "FORWARD_ONLY", "TUNNEL_ONLY")

        self.proxy_origin = url_to_origin(proxy_url)
        self.proxy_headers = [] if proxy_headers is None else proxy_headers
        self.proxy_mode = proxy_mode
        super().__init__(
            ssl_context=ssl_context,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
            http2=http2,
            backend=backend,
            max_keepalive=max_keepalive,
        )

    def request(
        self,
        method: bytes,
        url: URL,
        headers: Headers = None,
        stream: SyncByteStream = None,
        ext: dict = None,
    ) -> Tuple[int, Headers, SyncByteStream, dict]:
        if self._keepalive_expiry is not None:
            self._keepalive_sweep()

        if (
            self.proxy_mode == "DEFAULT" and url[0] == b"http"
        ) or self.proxy_mode == "FORWARD_ONLY":
            # By default HTTP requests should be forwarded.
            logger.trace(
                "forward_request proxy_origin=%r proxy_headers=%r method=%r url=%r",
                self.proxy_origin,
                self.proxy_headers,
                method,
                url,
            )
            return self._forward_request(
                method, url, headers=headers, stream=stream, ext=ext
            )
        else:
            # By default HTTPS should be tunnelled.
            logger.trace(
                "tunnel_request proxy_origin=%r proxy_headers=%r method=%r url=%r",
                self.proxy_origin,
                self.proxy_headers,
                method,
                url,
            )
            return self._tunnel_request(
                method, url, headers=headers, stream=stream, ext=ext
            )

    def _forward_request(
        self,
        method: bytes,
        url: URL,
        headers: Headers = None,
        stream: SyncByteStream = None,
        ext: dict = None,
    ) -> Tuple[int, Headers, SyncByteStream, dict]:
        """
        Forwarded proxy requests include the entire URL as the HTTP target,
        rather than just the path.
        """
        ext = {} if ext is None else ext
        timeout = cast(TimeoutDict, ext.get("timeout", {}))
        origin = self.proxy_origin
        connection = self._get_connection_from_pool(origin)

        if connection is None:
            connection = SyncHTTPConnection(
                origin=origin, http2=self._http2, ssl_context=self._ssl_context
            )
            self._add_to_pool(connection, timeout)

        # Issue a forwarded proxy request...

        # GET https://www.example.org/path HTTP/1.1
        # [proxy headers]
        # [headers]
        scheme, host, port, path = url
        if port is None:
            target = b"%b://%b%b" % (scheme, host, path)
        else:
            target = b"%b://%b:%d%b" % (scheme, host, port, path)

        url = self.proxy_origin + (target,)
        headers = merge_headers(self.proxy_headers, headers)

        (status_code, headers, stream, ext) = connection.request(
            method, url, headers=headers, stream=stream, ext=ext
        )

        wrapped_stream = ResponseByteStream(
            stream, connection=connection, callback=self._response_closed
        )

        return status_code, headers, wrapped_stream, ext

    def _tunnel_request(
        self,
        method: bytes,
        url: URL,
        headers: Headers = None,
        stream: SyncByteStream = None,
        ext: dict = None,
    ) -> Tuple[int, Headers, SyncByteStream, dict]:
        """
        Tunnelled proxy requests require an initial CONNECT request to
        establish the connection, and then send regular requests.
        """
        ext = {} if ext is None else ext
        timeout = cast(TimeoutDict, ext.get("timeout", {}))
        origin = url_to_origin(url)
        connection = self._get_connection_from_pool(origin)

        if connection is None:
            scheme, host, port = origin

            # First, create a connection to the proxy server
            proxy_connection = SyncHTTPConnection(
                origin=self.proxy_origin,
                http2=self._http2,
                ssl_context=self._ssl_context,
            )

            # Issue a CONNECT request...

            # CONNECT www.example.org:80 HTTP/1.1
            # [proxy-headers]
            target = b"%b:%d" % (host, port)
            connect_url = self.proxy_origin + (target,)
            connect_headers = [(b"Host", target), (b"Accept", b"*/*")]
            connect_headers = merge_headers(connect_headers, self.proxy_headers)

            try:
                (
                    proxy_status_code,
                    _,
                    proxy_stream,
                    _,
                ) = proxy_connection.request(
                    b"CONNECT", connect_url, headers=connect_headers, ext=ext
                )

                proxy_reason = get_reason_phrase(proxy_status_code)
                logger.trace(
                    "tunnel_response proxy_status_code=%r proxy_reason=%r ",
                    proxy_status_code,
                    proxy_reason,
                )
                # Read the response data without closing the socket
                for _ in proxy_stream:
                    pass

                # See if the tunnel was successfully established.
                if proxy_status_code < 200 or proxy_status_code > 299:
                    msg = "%d %s" % (proxy_status_code, proxy_reason)
                    raise ProxyError(msg)

                # Upgrade to TLS if required
                # We assume the target speaks TLS on the specified port
                if scheme == b"https":
                    proxy_connection.start_tls(host, timeout)
            except Exception as exc:
                proxy_connection.close()
                raise ProxyError(exc)

            # The CONNECT request is successful, so we have now SWITCHED PROTOCOLS.
            # This means the proxy connection is now unusable, and we must create
            # a new one for regular requests, making sure to use the same socket to
            # retain the tunnel.
            connection = SyncHTTPConnection(
                origin=origin,
                http2=self._http2,
                ssl_context=self._ssl_context,
                socket=proxy_connection.socket,
            )
            self._add_to_pool(connection, timeout)

        # Once the connection has been established we can send requests on
        # it as normal.
        (status_code, headers, stream, ext) = connection.request(
            method,
            url,
            headers=headers,
            stream=stream,
            ext=ext,
        )

        wrapped_stream = ResponseByteStream(
            stream, connection=connection, callback=self._response_closed
        )

        return status_code, headers, wrapped_stream, ext

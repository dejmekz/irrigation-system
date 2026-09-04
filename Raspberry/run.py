from app import create_app, socketio
from typing import Any, cast

app = create_app()

if __name__ == '__main__':
    web: dict[str, Any] = cast(dict[str, Any], app.config.get('WEB_CFG', {}))
    socketio.run( # type: ignore
        app,
        host=web.get('host', '0.0.0.0'),
        port=web.get('port', 5000),
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )

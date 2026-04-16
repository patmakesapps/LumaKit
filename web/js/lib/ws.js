/**
 * WebSocket connection manager — connect, reconnect, message routing.
 */

export class WS {
    constructor(handlers = {}) {
        this.handlers = handlers;
        this.ws = null;
        this.connected = false;
        this._reconnectTimer = null;
        this._reconnectDelay = 1000;
    }

    connect() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${location.host}/ws`;

        this.ws = new WebSocket(url);

        this.ws.onopen = () => {
            this.connected = true;
            this._reconnectDelay = 1000;
            if (this.handlers.onConnect) this.handlers.onConnect();
        };

        this.ws.onclose = () => {
            this.connected = false;
            if (this.handlers.onDisconnect) this.handlers.onDisconnect();
            this._scheduleReconnect();
        };

        this.ws.onerror = () => {
            // onclose will fire after this
        };

        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                const handler = this.handlers[data.type];
                if (handler) {
                    handler(data);
                }
            } catch (e) {
                console.error('WS message parse error:', e);
            }
        };
    }

    send(data) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(data));
        }
    }

    _scheduleReconnect() {
        if (this._reconnectTimer) return;
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            this._reconnectDelay = Math.min(this._reconnectDelay * 1.5, 10000);
            this.connect();
        }, this._reconnectDelay);
    }
}

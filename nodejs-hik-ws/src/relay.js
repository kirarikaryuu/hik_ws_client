import { createServer } from 'http';
import { WebSocketServer, WebSocket } from 'ws';
import { HikMediaClient } from './client.js';
import { parseProxyURL } from './config.js';

export class HikRelayServer {
  constructor(options = {}) {
    this.port = options.port || 8080;
    this.host = options.host || '0.0.0.0';
    this.httpServer = null;
    this.wss = null;
    this.sessions = new Map();
    this.sessionCounter = 0;
  }

  start() {
    this.httpServer = createServer((req, res) => {
      if (req.url === '/health') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', sessions: this.sessions.size }));
        return;
      }
      res.writeHead(404);
      res.end('Not Found');
    });

    this.wss = new WebSocketServer({ server: this.httpServer });

    this.wss.on('connection', (clientWs, req) => {
      this._handleClientConnection(clientWs, req);
    });

    this.httpServer.listen(this.port, this.host, () => {
      console.log(`Hikvision WS Relay Server listening on ws://${this.host}:${this.port}`);
    });

    return this;
  }

  _handleClientConnection(clientWs, req) {
    const url = new URL(req.url, `http://${req.headers.host}`);
    const proxyURL = url.searchParams.get('url');

    if (!proxyURL) {
      clientWs.close(1008, 'Missing ?url= parameter');
      return;
    }

    let config;
    try {
      config = parseProxyURL(proxyURL);
    } catch (err) {
      clientWs.close(1008, `Invalid URL: ${err.message}`);
      return;
    }

    const sessionId = ++this.sessionCounter;
    const session = {
      id: sessionId,
      clientWs,
      hikClient: null,
      stats: { videoBytes: 0, audioBytes: 0, frames: 0, startTime: Date.now() },
    };
    this.sessions.set(sessionId, session);

    console.log(`[Session ${sessionId}] New client connecting to ${config.deviceIP}:${config.devicePort}`);

    const hikClient = new HikMediaClient(config);
    session.hikClient = hikClient;

    hikClient.on('connected', () => {
      console.log(`[Session ${sessionId}] Connected to Hikvision proxy`);
      this._sendToClient(session, { type: 'status', status: 'connected' });
    });

    hikClient.on('authenticated', ({ pkd, rand }) => {
      console.log(`[Session ${sessionId}] Authenticated (PKD len=${pkd.length}, rand=${rand})`);
      this._sendToClient(session, { type: 'status', status: 'authenticated' });
    });

    hikClient.on('sdp', (sdpInfo) => {
      console.log(`[Session ${sessionId}] SDP received: ${sdpInfo.videoCodec || 'unknown'}`);
      this._sendToClient(session, {
        type: 'sdp',
        sdp: hikClient.sdp,
        info: {
          videoCodec: sdpInfo.videoCodec,
          videoClockRate: sdpInfo.videoClockRate,
          audioCodec: sdpInfo.audioCodec,
          audioClockRate: sdpInfo.audioClockRate,
        },
      });
    });

    hikClient.on('video', (data) => {
      session.stats.videoBytes += data.length;
      session.stats.frames++;

      if (clientWs.readyState === WebSocket.OPEN) {
        clientWs.send(data);
      }
    });

    hikClient.on('audio', (data) => {
      session.stats.audioBytes += data.length;

      if (clientWs.readyState === WebSocket.OPEN) {
        clientWs.send(data);
      }
    });

    hikClient.on('error', (err) => {
      console.error(`[Session ${sessionId}] Error: ${err.message}`);
      this._sendToClient(session, { type: 'error', message: err.message });
    });

    hikClient.on('close', () => {
      console.log(`[Session ${sessionId}] Hikvision connection closed`);
      this._cleanupSession(sessionId);
    });

    clientWs.on('close', () => {
      console.log(`[Session ${sessionId}] Client disconnected`);
      this._cleanupSession(sessionId);
    });

    clientWs.on('error', (err) => {
      console.error(`[Session ${sessionId}] Client WebSocket error: ${err.message}`);
    });

    hikClient.run().catch((err) => {
      console.error(`[Session ${sessionId}] Run error: ${err.message}`);
      this._sendToClient(session, { type: 'error', message: err.message });
      clientWs.close(1011, err.message);
    });
  }

  _sendToClient(session, obj) {
    if (session.clientWs.readyState === WebSocket.OPEN) {
      session.clientWs.send(JSON.stringify(obj));
    }
  }

  _cleanupSession(sessionId) {
    const session = this.sessions.get(sessionId);
    if (!session) return;

    if (session.hikClient) {
      session.hikClient.close();
      session.hikClient = null;
    }

    const duration = Date.now() - session.stats.startTime;
    console.log(
      `[Session ${sessionId}] Closed. ` +
      `Duration: ${(duration / 1000).toFixed(1)}s, ` +
      `Frames: ${session.stats.frames}, ` +
      `Video: ${(session.stats.videoBytes / 1024).toFixed(1)}KB, ` +
      `Audio: ${(session.stats.audioBytes / 1024).toFixed(1)}KB`
    );

    this.sessions.delete(sessionId);
  }

  stop() {
    for (const [id, session] of this.sessions) {
      this._cleanupSession(id);
    }
    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }
    if (this.httpServer) {
      this.httpServer.close();
      this.httpServer = null;
    }
  }
}

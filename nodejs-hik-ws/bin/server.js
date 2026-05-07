import { HikRelayServer } from '../src/relay.js';

const port = parseInt(process.env.PORT || '8080', 10);
const host = process.env.HOST || '0.0.0.0';

const server = new HikRelayServer({ port, host });
server.start();

process.on('SIGINT', () => {
  console.log('\nShutting down...');
  server.stop();
  process.exit(0);
});

process.on('SIGTERM', () => {
  server.stop();
  process.exit(0);
});

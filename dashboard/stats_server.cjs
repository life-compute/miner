/**
 * Tiny HTTP server — serves stats.json to the dashboard at http://localhost:8765/stats.json
 * (Vite proxies /stats.json → 8765 in dev mode)
 */
const http = require('http');
const fs   = require('fs');
const path = require('path');

const PORT      = 8765;
const STATS     = path.join(__dirname, '..', 'stats.json');
const CORS_HDRS = {
  'Access-Control-Allow-Origin': '*',
  'Content-Type': 'application/json',
};

http.createServer((req, res) => {
  if (req.url === '/stats.json') {
    try {
      res.writeHead(200, CORS_HDRS);
      res.end(fs.readFileSync(STATS));
    } catch {
      res.writeHead(500); res.end('{}');
    }
  } else {
    res.writeHead(404); res.end();
  }
}).listen(PORT, () => console.log(`stats server → http://localhost:${PORT}/stats.json`));

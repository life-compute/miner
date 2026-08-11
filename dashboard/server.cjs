/**
 * LIFE Compute Dashboard — static file server on :3001
 * Serves the pre-built Vite dist/ and proxies /stats.json from live stats file.
 */
const http = require('http');
const fs   = require('fs');
const path = require('path');

const PORT      = parseInt(process.env.DASHBOARD_PORT || '3001');
const DIST      = path.join(__dirname, 'dist');
const STATS     = path.join(__dirname, '..', 'stats.json');

const MIME = {
  '.html': 'text/html',
  '.js':   'application/javascript',
  '.css':  'text/css',
  '.json': 'application/json',
  '.ico':  'image/x-icon',
  '.svg':  'image/svg+xml',
};

http.createServer((req, res) => {
  // Live stats endpoint
  if (req.url === '/stats.json') {
    try {
      res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end(fs.readFileSync(STATS));
    } catch { res.writeHead(500); res.end('{}'); }
    return;
  }

  // Static files from dist/
  let filePath = path.join(DIST, req.url === '/' ? 'index.html' : req.url);
  if (!fs.existsSync(filePath)) filePath = path.join(DIST, 'index.html'); // SPA fallback

  const ext  = path.extname(filePath);
  const mime = MIME[ext] || 'application/octet-stream';
  try {
    res.writeHead(200, { 'Content-Type': mime });
    res.end(fs.readFileSync(filePath));
  } catch { res.writeHead(404); res.end('Not found'); }

}).listen(PORT, () => {
  console.log(`LIFE Compute dashboard → http://localhost:${PORT}`);
  console.log(`Stats live feed       → http://localhost:${PORT}/stats.json`);
});

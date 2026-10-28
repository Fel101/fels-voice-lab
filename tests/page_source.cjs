// The Voice Lab page as one source string: serve.html followed by each script it loads from the project folder.
// Tests that look for a function or a control use this, so they still find it now that the page script is split.
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');

function pageSource() {
  const html = fs.readFileSync(path.join(root, 'serve.html'), 'utf8');
  const scripts = [...html.matchAll(/<script[^>]*\ssrc="\/([^"]+)"/g)].map((match) => match[1]);
  return [html, ...scripts.map((name) => fs.readFileSync(path.join(root, name), 'utf8'))].join('\n');
}

module.exports = {pageSource};

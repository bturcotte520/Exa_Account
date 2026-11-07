'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Small DOM double: the formatter must use text nodes, never an HTML parser.
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.value = ''; }
  set textContent(value) { this.children = []; this.value = String(value); }
  get textContent() { return this.value + this.children.map((child) => child.textContent).join(''); }
  set innerHTML(_) { throw new Error('Unsafe HTML rendering'); }
  append(...children) { this.children.push(...children); }
}
const context = vm.createContext({ URL, document: {
  createElement: (tag) => new Element(tag),
  createTextNode: (value) => { const text = new Element('#text'); text.textContent = value; return text; },
} });
const script = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
vm.runInContext(script.slice(0, script.indexOf("\ndocument.querySelectorAll('[data-close]')")), context);
const render = (value) => context.research(value);
const descendants = (element, tag) => element.children.flatMap((child) => [
  ...(child.tagName === tag ? [child] : []), ...descendants(child, tag),
]);

test('paragraphs, headings, inline emphasis, ordered and nested bullet lists', () => {
  const content = render('# Research\n\nFirst **supported** paragraph.\nSecond line.\n\nNext paragraph.\n\n- Site A\n  - Address A\n- Site B\n\n3. Verify source\n4. Review');
  assert.equal(descendants(content, 'h4')[0].textContent, 'Research');
  assert.equal(descendants(content, 'p').length, 2);
  assert.equal(descendants(content, 'strong')[0].textContent, 'supported');
  assert.equal(descendants(content, 'ul').length, 2);
  assert.equal(descendants(content, 'li').length, 5);
  assert.equal(descendants(content, 'ol')[0].start, 3);
  assert.match(content.textContent, /First supported paragraph.\nSecond line./);
});

test('escaped API layout is decoded without unescaping paths or inline code', () => {
  const content = render('Summary\\n\\n## Sites\\n- First\\n- Second\n\nKeep `C:\\new\\notes` and C:\\new\\notes or C:\\tools\\report.');
  assert.equal(descendants(content, 'h5')[0].textContent, 'Sites');
  assert.equal(descendants(content, 'li').length, 2);
  assert.match(content.textContent, /C:\\new\\notes/);
  assert.match(content.textContent, /C:\\tools\\report/);
  assert.equal(content.textContent.match(/C:\\new\\notes/g).length, 2);
  assert.equal(context.researchLines('The literal \\n is a newline escape. C:\\new\\notes'), 'The literal \\n is a newline escape. C:\\new\\notes');
  assert.equal(context.researchLines('A\\r\\n\\r\\nB'), 'A\n\nB');
});

test('HTML stays literal; only credential-free HTTP(S) links are active', () => {
  const content = render('<img src=x onerror=alert(1)>\n\n[Unsafe](javascript:alert) [Data](data:text/html,bad) [Secret](https://user:pass@example.com) [Source](https://example.com/page)');
  assert.equal(descendants(content, 'img').length, 0);
  assert.match(content.textContent, /<img src=x onerror=alert\(1\)>/);
  const links = descendants(content, 'a');
  assert.equal(links.length, 1);
  assert.equal(links[0].href, 'https://example.com/page');
  assert.equal(links[0].rel, 'noopener noreferrer');
  assert.match(content.textContent, /\[Unsafe\]\(javascript:alert\)/);
});

test('structured content uses labelled fields and lists, never JSON prose', () => {
  const content = render({ address: { street: '123 Test St', city: 'Test City' }, excerpt: ['Passage one', 'Passage two'], source_url: 'https://example.com' });
  assert.equal(descendants(content, 'dl').length, 2);
  assert.equal(descendants(content, 'dt').length, 5);
  assert.equal(descendants(content, 'li').length, 2);
  assert.equal(descendants(content, 'a').length, 1);
  assert.doesNotMatch(content.textContent, /\[object Object\]|\{"/);
  assert.match(content.textContent, /123 Test St/);
});

test('plain prose is not rewritten or split at arbitrary sentence counts', () => {
  const prose = 'One. Two. Three. Four. Keep the actual source text.';
  assert.equal(render(prose).textContent, prose);
  assert.equal(descendants(render(prose), 'p').length, 1);
  assert.equal(descendants(render('• Alpha\n• Beta'), 'li').length, 2);
  assert.equal(descendants(render('```text\n<img>\n- literal\n```'), 'li').length, 0);
  assert.equal(descendants(render('```text\n<img>\n- literal\n```'), 'code')[0].textContent, '<img>\n- literal');
  assert.equal(descendants(render('```\n\nline\n\n```'), 'code')[0].textContent, '\nline\n');
});

import hljs from "highlight.js/lib/core";
import json from "highlight.js/lib/languages/json";
import yaml from "highlight.js/lib/languages/yaml";
import bash from "highlight.js/lib/languages/bash";
import stringify from "json-stringify-pretty-compact";

hljs.registerLanguage("json", json);
hljs.registerLanguage("yaml", yaml);
hljs.registerLanguage("bash", bash);

const selector = "pre[data-format]";
const rendered = new WeakMap();
const maxLength = 200000;

function format(element) {
  const source = element.textContent;
  if (rendered.get(element) === source) return;
  const language = element.dataset.format;
  if (!hljs.getLanguage(language)) throw new Error(`Unsupported output format: ${language}`);
  if (!source.trim()) return;
  if (source.length > maxLength) {
    element.dataset.formatStatus = "large";
    element.title = "Large output: original text retained for responsive viewing.";
    return;
  }
  let text = source;
  if (language === "json") {
    if (!/^\s*[{[]/.test(source)) return;
    let value;
    try {
      value = JSON.parse(source);
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      element.dataset.formatStatus = "invalid";
      element.title = "Invalid JSON: showing the original text.";
      return;
    }
    // Keep numeric spellings that JSON.parse/stringify cannot round-trip exactly.
    const numbers = source.match(/"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g) || [];
    const lossless = numbers.every(token => token.startsWith('"') || JSON.stringify(Number(token)) === token);
    if (element.dataset.compact !== "false" && lossless) {
      text = stringify(value, { maxLength: 100, indent: 2 });
    }
  }
  element.classList.add("structured-output");
  element.dataset.formatStatus = "formatted";
  element.removeAttribute("title");
  rendered.set(element, text);
  element.innerHTML = hljs.highlight(text, { language, ignoreIllegals: true }).value;
}

function scan(root) {
  if (root instanceof Element && root.matches(selector)) format(root);
  root.querySelectorAll(selector).forEach(format);
}

scan(document);
const observer = new MutationObserver(records => {
  const changed = new Set();
  for (const record of records) {
    const parent = record.target instanceof Element ? record.target : record.target.parentElement;
    const output = parent?.closest(selector);
    if (output) changed.add(output);
    for (const node of record.addedNodes) {
      if (node instanceof Element) scan(node);
    }
  }
  changed.forEach(format);
});
observer.observe(document.body, { childList: true, characterData: true, subtree: true });

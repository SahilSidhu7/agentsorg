// Tiny, safe Markdown renderer for agent replies. Everything is escaped first; only a fixed set of
// tags is produced (headings, lists, tables, quotes, code, bold/italic, http(s) links).
const md = (() => {
  const E = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function inline(s) {
    const codes = [];
    s = E(s).replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>')
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>").replace(/(^|[^_\w])_([^_\s][^_]*?)_(?!\w)/g, "$1<em>$2</em>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
  }
  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  return function (src) {
    const lines = String(src ?? "").replace(/\r/g, "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (/^\s*```/.test(l)) {                                   // fenced code
        const buf = []; i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
        i++; out.push(`<pre><code>${E(buf.join("\n"))}</code></pre>`); continue;
      }
      if (!l.trim()) { i++; continue; }
      let m;
      if ((m = l.match(/^\s*(#{1,6})\s+(.*)$/))) { const n = Math.min(m[1].length + 1, 6); out.push(`<h${n}>${inline(m[2])}</h${n}>`); i++; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
      if (l.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const head = cells(l); i += 2; const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
        out.push(`<div class="mdt"><table><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</table></div>`);
        continue;
      }
      if (/^\s*>/.test(l)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push(`<blockquote>${md(buf.join("\n"))}</blockquote>`); continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) {
        const ordered = /^\s*\d/.test(l), items = [];
        while (i < lines.length && (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
          const t = lines[i++];
          if (/^\s*([-*+]|\d+[.)])\s+/.test(t)) items.push([t.match(/^\s*/)[0].length, t.replace(/^\s*([-*+]|\d+[.)])\s+/, "")]);
          else items[items.length - 1][1] += " " + t.trim();
        }
        const tag = ordered ? "ol" : "ul";
        out.push(`<${tag}>${items.map(([ind, t]) => `<li${ind >= 2 ? ' class="sub"' : ""}>${inline(t.replace(/^\[ \]\s/, "☐ ").replace(/^\[x\]\s/i, "☑ "))}</li>`).join("")}</${tag}>`);
        continue;
      }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^\s*(```|#{1,6}\s|>|([-*+]|\d+[.)])\s+)/.test(lines[i])
             && !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))) buf.push(inline(lines[i++]));
      if (!buf.length) buf.push(inline(lines[i++]));               // never stall on an odd line
      out.push(`<p>${buf.join("<br>")}</p>`);
    }
    return out.join("");
  };
})();

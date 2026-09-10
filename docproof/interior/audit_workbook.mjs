// Render a deterministic audit projection. This process has no network calls.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const [input, output, modules, renderDir] = process.argv.slice(2);
const require = createRequire(modules ? path.join(modules, 'docproof-audit.cjs') : import.meta.url);
const { Workbook, SpreadsheetFile } = await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const data = JSON.parse(await fs.readFile(input, 'utf8'));
const wb = Workbook.create();
const columns = {
  Corrections: ['Instruction', 'Status', 'Requested correction / source wording', 'Reason / remaining work', 'Saved text confirmed', 'Formatting confirmed', 'Source files', 'Source locations', 'Edit IDs', 'Evidence IDs', 'Final review coverage', 'Book outcome'],
  Changes: ['Instruction', 'Edit', 'Story', 'Original text', 'Requested replacement', 'Saved replacement', 'Expected occurrences', 'Confirmed occurrences', 'Text confirmed', 'Formatting confirmed', 'Requested formatting', 'Original offsets (0-based)'],
  Evidence: ['Evidence', 'Source file', 'Location', 'Required', 'Coverage', 'Instructions', 'Complete extracted content', 'Source ID'],
  Files: ['Slot', 'Submission', 'Filename', 'Receipt', 'Duplicate of slot', 'Bytes', 'SHA-256', 'File ID', 'Source IDs', 'Read errors'],
  'Run details': ['Check', 'Result'],
};
const keys = ['corrections', 'changes', 'evidence', 'files', 'details'];
const literal = value => typeof value === 'string'
  ? value.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, c => `[U+${c.charCodeAt(0).toString(16).padStart(4, '0')}]`)
  : value ?? null;
function textMatrix(sheet, address, matrix) {
  // Quoted string expressions prevent formula execution AND automatic date/
  // number conversion in the authoring library. Doubled quotes keep all source
  // wording literal, including strings beginning with =, +, -, @ or a quote.
  const range = sheet.getRange(address);
  range.formulas = matrix.map(row => row.map(value => typeof value === 'string' ? `="${value.replaceAll('"', '""')}"` : ''));
  matrix.forEach((row, r) => row.forEach((value, c) => {
    if (value !== null && typeof value !== 'string') range.getCell(r, c).values = [[value]];
  }));
}
function letter(n) {
  let s = '';
  for (n++; n; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + (n - 1) % 26) + s;
  return s;
}
const index = [];
for (const [i, [name, headers]] of Object.entries(columns).entries()) {
  const sheet = wb.worksheets.add(name);
  sheet.showGridLines = false;
  const rows = [];
  for (const item of data[keys[i]]) {
    // Continuation rows retain IDs and part numbers. No source text is silently
    // truncated at Excel's cell limit, or concealed in a maximum-height row.
    const parts = headers.map(h => typeof item[h] === 'string' ? item[h].match(/[\s\S]{1,700}/g) || [''] : [item[h] ?? null]);
    for (let part = 0; part < Math.max(...parts.map(p => p.length)); part++) {
      const row = parts.map(p => p.length === 1 ? p[0] : p[part] ?? '');
      rows.push([...row.map(literal), part + 1]);
    }
  }
  headers.push('Part');
  const last = letter(headers.length - 1), end = 9 + rows.length;
  const all = sheet.getRange(`A1:${last}${Math.max(10, end)}`);
  all.format.font = { name: 'Arial', size: 10, color: '#172B3A' };
  all.format.rowHeight = 20;
  all.format.columnWidth = 22;
  all.format.verticalAlignment = 'top';
  all.format.wrapText = true;
  sheet.getRange('A2:D2').merge();
  sheet.getRange('A2').values = [[name === 'Corrections' ? 'InDesign correction audit' : name]];
  sheet.getRange('A2').format.font = { name: 'Arial', size: 14, bold: true, color: '#172B3A' };
  sheet.getRange('A2:D2').format.borders = { bottom: { style: 'thin', color: '#172B3A' } };
  sheet.getRange('A2:D2').format.rowHeight = 28;
  textMatrix(sheet, 'A4:B6', [
    ['Source book', literal(data.source_book || 'Not established')],
    ['Output book', literal(data.output_book || 'Not established')],
    ['Book outcome', literal(data.outcome.replaceAll('_', ' '))],
  ]);
  sheet.getRange('B4:D4').merge();
  sheet.getRange('B5:D5').merge();
  sheet.getRange('B6:D6').merge();
  sheet.getRange('A4:A6').format.font = { name: 'Arial', size: 10, bold: true };
  sheet.getRange(`A9:${last}9`).values = [headers];
  sheet.getRange(`A9:${last}9`).format = { fill: '#243D4D', font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' }, wrapText: true, rowHeight: 40 };
  if (rows.length) {
    textMatrix(sheet, `A10:${last}${end}`, rows);
    const table = sheet.tables.add(`A9:${last}${end}`, true, `Audit${i + 1}`);
    table.showFilterButton = true;
    for (let r = 10; r <= end; r++) {
      if (r % 2 === 0) sheet.getRange(`A${r}:${last}${r}`).format.fill = '#F1F5F7';
    }
  }
  headers.forEach((h, col) => {
    const c = letter(col);
    const width = /text|wording|replacement|remaining work|content|Result|errors|formatting$/.test(h) ? 52
      : /SHA|Evidence|Source IDs|Source file|Filename/.test(h) ? 36 : /Status|Receipt|Check/.test(h) ? 30
      : /occurrences|confirmed/.test(h) ? 16 : /Part|Slot|Story/.test(h) ? 9 : 23;
    sheet.getRange(`${c}1:${c}${Math.max(10, end)}`).format.columnWidth = col === 0 ? Math.max(23, width) : width;
    if (['Part', 'Bytes', 'Expected occurrences', 'Confirmed occurrences'].includes(h))
      sheet.getRange(`${c}10:${c}${Math.max(10, end)}`).setNumberFormat('#,##0');
  });
  if (rows.length) sheet.getRange(`A10:${last}${end}`).format.autofitRows();
  sheet.freezePanes.freezeRows(9);
  sheet.freezePanes.freezeColumns(name === 'Corrections' ? 2 : 1);
  if (name === 'Corrections') {
    sheet.getRange('A7:D7').merge();
    sheet.getRange('A7').values = [[data.instruction_count === null ? 'Correction count unknown; rows record intake or evidence that still needs attention.' : `Items identified: ${data.instruction_count}. Includes context and requests requiring no edit.`]];
    sheet.getRange('A7:D7').format.rowHeight = 30;
    sheet.getRange('F4:G7').values = [['Done — verified', null], ['Applied — book held', null], ['Unconfirmed / not done', null], ['No edit', null]];
    sheet.getRange('F4:G7').format.rowHeight = 34;
    if (rows.length) {
      const part = `${last}10:${last}${end}`;
      sheet.getRange('G4:G7').formulas = [
        [`=COUNTIFS(B10:B${end},"Done — verified",${part},1)`],
        [`=COUNTIFS(B10:B${end},"Applied — book review required",${part},1)`],
        [`=COUNTIF(${part},1)-G4-G5-G7`],
        [`=COUNTIFS(B10:B${end},"No edit — reviewed",${part},1)+COUNTIFS(B10:B${end},"No edit — planner assessment only",${part},1)`],
      ];
    }
    sheet.getRange('A8:D8').merge();
    sheet.getRange('A8').values = [['Filter Status for remaining work. Run details lists book-wide blockers. Part rows continue long text.']];
    sheet.getRange('A8:D8').format.rowHeight = 30;
  }
  index.push({ sheetName: name, rows: rows.length });
}
wb.recalculate();
if (renderDir) {
  await fs.mkdir(renderDir, { recursive: true });
  for (const { sheetName } of index) {
    const preview = await wb.render({ sheetName, range: 'A1:G12', scale: 1, format: 'png' });
    await fs.writeFile(path.join(renderDir, `${sheetName}.png`), new Uint8Array(await preview.arrayBuffer()));
  }
  const inspected = await wb.inspect({ kind: 'region', sheetId: 'Corrections', range: 'F4:G7', maxChars: 1800 });
  await fs.writeFile(path.join(renderDir, 'verification.txt'), inspected.ndjson || JSON.stringify(inspected));
}
await (await SpreadsheetFile.exportXlsx(wb)).save(output);

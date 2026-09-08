document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("th[data-sort]").forEach(th => {
    th.style.cursor = "pointer";
    th.addEventListener("click", () => sortTable(th));
  });
});

/**
 * Re-initialize sort handlers for a specific table.
 * Call after dynamically replacing thead content.
 */
function initSort(table) {
  if (!table) return;
  table.querySelectorAll("th[data-sort]").forEach(th => {
    th.style.cursor = "pointer";
    // Remove existing listener by cloning (prevents double-bind)
    const newTh = th.cloneNode(true);
    th.parentNode.replaceChild(newTh, th);
    newTh.addEventListener("click", () => sortTable(newTh));
  });
}

function sortTable(th) {
  const table = th.closest("table");
  const tbody = table.querySelector("tbody");
  const idx = Array.from(th.parentNode.children).indexOf(th);
  const type = th.dataset.sort;
  const rows = Array.from(tbody.querySelectorAll("tr"));

  const asc = th.classList.contains("sort-asc");
  table.querySelectorAll("th").forEach(h => h.classList.remove("sort-asc", "sort-desc"));
  th.classList.add(asc ? "sort-desc" : "sort-asc");
  const dir = asc ? -1 : 1;

  rows.sort((a, b) => {
    const cellA = a.children[idx], cellB = b.children[idx];
    if (type === "num") {
      const va = parseFloat(cellA.dataset.sortValue ?? cellA.textContent.replace(/[$M,k]/gi, "")) || 0;
      const vb = parseFloat(cellB.dataset.sortValue ?? cellB.textContent.replace(/[$M,k]/gi, "")) || 0;
      return (va - vb) * dir;
    }
    if (type === "pos") {
      const va = parseInt(cellA.dataset.sortValue) || 99;
      const vb = parseInt(cellB.dataset.sortValue) || 99;
      return (va - vb) * dir;
    }
    return cellA.textContent.trim().localeCompare(cellB.textContent.trim()) * dir;
  });

  rows.forEach(r => tbody.appendChild(r));

  // Re-number rank column if present
  const firstTh = table.querySelector("thead th");
  if (firstTh && firstTh.textContent.trim() === "#") {
    const n = rows.length;
    // Apply smart numbering to numeric columns and any column with data-sort-best
    if (type === "num" || th.dataset.sortBest) {
      const bestIsLow = th.dataset.sortBest === "low";
      const descending = th.classList.contains("sort-desc");
      const topIsFirst = descending !== bestIsLow;
      rows.forEach((r, i) => {
        r.children[0].textContent = topIsFirst ? i + 1 : n - i;
      });
    } else {
      rows.forEach((r, i) => { r.children[0].textContent = i + 1; });
    }
  }
}

/**
 * Export visible table rows as CSV and trigger download.
 * @param {string} tableId - ID of the table element
 * @param {string} filename - Download filename (default: 'export.csv')
 */
function exportTableCSV(tableId, filename) {
  var table = document.getElementById(tableId);
  if (!table) return;
  filename = filename || 'export.csv';

  var rows = [];

  // Header row
  var thead = table.querySelector('thead tr');
  if (thead) {
    var headers = [];
    thead.querySelectorAll('th').forEach(function(th) {
      var text = th.textContent.trim();
      // Skip non-data columns (compare checkboxes, etc.)
      if (text === '⚖') return;
      headers.push('"' + text.replace(/"/g, '""') + '"');
    });
    rows.push(headers.join(','));
  }

  // Data rows (only visible)
  var skipFirst = thead && thead.querySelector('th') && thead.querySelector('th').textContent.trim() === '⚖';
  table.querySelectorAll('tbody tr').forEach(function(tr) {
    if (tr.style.display === 'none') return;
    var cells = [];
    tr.querySelectorAll('td').forEach(function(td, i) {
      if (skipFirst && i === 0) return;
      var val = td.getAttribute('data-sort-value') || td.textContent.trim();
      cells.push('"' + val.replace(/"/g, '""') + '"');
    });
    if (cells.length > 0) rows.push(cells.join(','));
  });

  var csv = rows.join('\n');
  var blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'});
  var link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

/**
 * Export visible rows from MULTIPLE tables (same column layout) into one
 * combined CSV — e.g. a "confirmed" + "needs scouting" table pair that
 * should download as a single file. Header is taken from the first table
 * found; later tables contribute only their data rows.
 * @param {string[]} tableIds - IDs of the table elements, in output order
 * @param {string} filename - Download filename (default: 'export.csv')
 */
function exportTablesCSV(tableIds, filename) {
  filename = filename || 'export.csv';
  var rows = [];
  var headerWritten = false;

  tableIds.forEach(function(tableId) {
    var table = document.getElementById(tableId);
    if (!table) return;

    var thead = table.querySelector('thead tr');
    var skipFirst = false;
    if (thead) {
      var firstTh = thead.querySelector('th');
      skipFirst = !!firstTh && firstTh.textContent.trim() === '';
      if (!headerWritten) {
        var headers = [];
        thead.querySelectorAll('th').forEach(function(th, i) {
          if (skipFirst && i === 0) return;
          var text = th.textContent.trim();
          headers.push('"' + text.replace(/"/g, '""') + '"');
        });
        rows.push(headers.join(','));
        headerWritten = true;
      }
    }

    table.querySelectorAll('tbody tr').forEach(function(tr) {
      if (tr.style.display === 'none') return;
      var cells = [];
      tr.querySelectorAll('td').forEach(function(td, i) {
        if (skipFirst && i === 0) return;
        var val = td.getAttribute('data-sort-value') || td.textContent.trim();
        cells.push('"' + val.replace(/"/g, '""') + '"');
      });
      if (cells.length > 0) rows.push(cells.join(','));
    });
  });

  if (rows.length === 0) return;
  var csv = rows.join('\n');
  var blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'});
  var link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

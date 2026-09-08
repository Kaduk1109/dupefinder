(function () {
  const body = document.body;
  const rootId = body.dataset.rootId;
  const rootPath = body.dataset.rootPath;
  const urlScanId = new URLSearchParams(window.location.search).get('scan_id');
  let scanId = urlScanId || body.dataset.scanId;
  let lastRotationPass = body.dataset.rotationPass === '1';

  const progressBar = document.getElementById('progress-bar-inner');
  const progressText = document.getElementById('progress-text');
  const stopScanBtn = document.getElementById('stop-scan-btn');
  const resumeScanBtn = document.getElementById('resume-scan-btn');
  const groupsContainer = document.getElementById('groups-container');
  const groupBySelect = document.getElementById('group-by');
  const showReviewedToggle = document.getElementById('show-reviewed-toggle');
  const deleteBtn = document.getElementById('delete-btn');
  const hideBtn = document.getElementById('hide-btn');
  const exportLink = document.getElementById('export-link');
  const importInput = document.getElementById('import-input');
  const reviewMsg = document.getElementById('review-msg');

  const summaryCard = document.getElementById('summary-card');
  const summaryGroups = document.getElementById('summary-groups');
  const summaryExtra = document.getElementById('summary-extra');
  const summarySpace = document.getElementById('summary-space');
  const summaryMarked = document.getElementById('summary-marked');
  const summaryTrashed = document.getElementById('summary-trashed');

  const pager = document.getElementById('pager');
  const pagerPrev = document.getElementById('pager-prev');
  const pagerNext = document.getElementById('pager-next');
  const pagerStatus = document.getElementById('pager-status');
  const PAGE_SIZE = 50; // duplicate GROUPS per page, not files -- keeps each
                         // page's table small regardless of library size
  let currentPage = 1;

  const previewCurrentImg = document.getElementById('preview-current-img');
  const previewCurrentCaption = document.getElementById('preview-current-caption');
  const previewPreviousImg = document.getElementById('preview-previous-img');
  const previewPreviousCaption = document.getElementById('preview-previous-caption');
  let currentPreview = null; // {src, caption}

  let pollTimer = null;
  let resultsLoadedOnce = false;

  function setScanButtons(scanStatus) {
    if (scanStatus === 'running') {
      stopScanBtn.style.display = '';
      resumeScanBtn.style.display = 'none';
    } else if (scanStatus === 'stopped' || scanStatus === 'error') {
      stopScanBtn.style.display = 'none';
      resumeScanBtn.style.display = '';
      resumeScanBtn.textContent = 'Resume scan';
    } else {
      stopScanBtn.style.display = 'none';
      resumeScanBtn.style.display = 'none';
    }
  }

  // ---- Scenario 12/13/14/15: progress + ETA polling, no websockets needed.
  async function pollProgress() {
    if (!scanId) {
      progressText.textContent = 'No scan associated with this folder yet.';
      loadGroups(); // still show whatever results exist from a prior scan
      return;
    }
    try {
      const res = await fetch(`/api/scan/progress/${scanId}`);
      if (!res.ok) throw new Error('progress fetch failed');
      const data = await res.json();
      lastRotationPass = !!data.rotation_pass;
      setScanButtons(data.scan_status);

      const pct = data.total > 0 ? Math.min(100, (data.processed / data.total) * 100) : 0;
      progressBar.style.width = pct.toFixed(1) + '%';

      let etaText = '';
      if (data.eta) {
        if (data.eta.status === 'calculating') {
          etaText = ' — calculating ETA...';
        } else if (data.eta.eta_human) {
          etaText = ` — about ${data.eta.eta_human} remaining (${data.eta.files_per_second}/s)`;
        }
      }
      progressText.textContent =
        `${data.processed.toLocaleString()} / ${data.total.toLocaleString()} processed ` +
        `(${data.phase})${etaText}`;

      if (data.scan_status === 'done') {
        progressText.textContent += ' — scan complete.';
        clearInterval(pollTimer);
        loadGroups();
        loadSummary();
      } else if (data.scan_status === 'stopped') {
        progressText.textContent += ' — stopped. Click Resume to continue from here.';
        clearInterval(pollTimer);
        loadGroups();
        loadSummary();
      } else if (data.scan_status === 'error') {
        progressText.textContent = 'Scan failed: ' + (data.error_message || 'unknown error');
        clearInterval(pollTimer);
      } else if (!resultsLoadedOnce && data.phase !== 'discovering') {
        // Show whatever results exist so far isn't meaningful mid-hash, but
        // once a scan is already 'done' from a previous run we still want
        // the table to load immediately (Scenario 5) rather than waiting.
      }
    } catch (err) {
      progressText.textContent = 'Could not fetch progress: ' + err;
    }
  }

  function restartPolling() {
    clearInterval(pollTimer);
    pollProgress();
    pollTimer = setInterval(pollProgress, 3000);
  }

  stopScanBtn.addEventListener('click', async () => {
    if (!scanId) return;
    stopScanBtn.disabled = true;
    progressText.textContent = 'Stopping... (finishing the current file)';
    try {
      await fetch(`/api/scan/stop/${scanId}`, {method: 'POST'});
    } finally {
      stopScanBtn.disabled = false;
      restartPolling();
    }
  });

  resumeScanBtn.addEventListener('click', async () => {
    resumeScanBtn.disabled = true;
    reviewMsg.textContent = 'Resuming scan...';
    try {
      const res = await fetch('/api/scan/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({folder: rootPath, rotation_pass: lastRotationPass})
      });
      const data = await res.json();
      if (!res.ok) { reviewMsg.textContent = 'Error: ' + data.error; return; }
      scanId = data.scan_id;
      reviewMsg.textContent = '';
      restartPolling();
    } finally {
      resumeScanBtn.disabled = false;
    }
  });

  // ---- Scenario 5: results load instantly from stored DB, not re-scanned.
  // Paginated at the GROUP level (see /api/groups) so a 270,000-file
  // library never means one giant table -- each page is a bounded, fast
  // request+render regardless of total library size.
  async function loadGroups() {
    resultsLoadedOnce = true;
    const groupBy = groupBySelect.value;
    const showReviewed = showReviewedToggle.checked ? '1' : '0';
    groupsContainer.textContent = 'Loading...';
    try {
      const res = await fetch(
        `/api/groups?root_id=${rootId}&group_by=${groupBy}&show_reviewed=${showReviewed}` +
        `&page=${currentPage}&page_size=${PAGE_SIZE}`
      );
      const data = await res.json();
      renderGroups(data.groups, groupBy);
      renderPager(data);
    } catch (err) {
      groupsContainer.textContent = 'Failed to load results: ' + err;
    }
  }

  function renderPager(data) {
    if (data.total_groups <= data.groups.length && data.page === 1) {
      // Everything fit on one page -- no point showing pager controls.
      pager.style.display = 'none';
      return;
    }
    pager.style.display = 'flex';
    pagerStatus.textContent =
      `Page ${data.page} of ${data.total_pages} (${data.total_groups.toLocaleString()} duplicate groups)`;
    pagerPrev.disabled = data.page <= 1;
    pagerNext.disabled = data.page >= data.total_pages;
  }

  pagerPrev.addEventListener('click', () => {
    if (currentPage > 1) { currentPage--; loadGroups(); window.scrollTo({top: 0, behavior: 'smooth'}); }
  });
  pagerNext.addEventListener('click', () => {
    currentPage++; loadGroups(); window.scrollTo({top: 0, behavior: 'smooth'});
  });

  function resetToFirstPage() {
    currentPage = 1;
    loadGroups();
  }

  function formatBytes(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = bytes, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
  }

  async function loadSummary() {
    try {
      const res = await fetch(`/api/summary?root_id=${rootId}`);
      if (!res.ok) return;
      const data = await res.json();
      summaryCard.style.display = '';
      summaryGroups.textContent = data.total_duplicate_groups.toLocaleString();
      summaryExtra.textContent = data.extra_copies.toLocaleString();
      summarySpace.textContent = formatBytes(data.potential_space_savings_bytes);
      summaryMarked.textContent = data.marked_for_delete_count.toLocaleString() +
        (data.marked_for_delete_count ? ` (${formatBytes(data.marked_for_delete_space_bytes)})` : '');
      summaryTrashed.textContent = data.already_trashed_count.toLocaleString() +
        (data.already_trashed_count ? ` (${formatBytes(data.already_trashed_space_bytes)})` : '');
    } catch (err) {
      // Summary is a nice-to-have -- a failed fetch here shouldn't disrupt the rest of the page.
    }
  }

  function renderGroups(groups, groupBy) {
    if (!groups.length) {
      groupsContainer.textContent = 'No results yet.';
      return;
    }
    // A group of size 1 isn't a duplicate under any of these groupings --
    // content/filename obviously, and a single photo on a given date isn't
    // meaningfully "grouped" either.
    const meaningfulGroups = groups.filter(g => g.files.length >= 2);
    if (!meaningfulGroups.length) {
      groupsContainer.innerHTML = '';
      groupsContainer.textContent = 'No duplicate groups found.';
      return;
    }

    const table = document.createElement('table');
    table.className = 'dup-table';
    table.innerHTML = `
      <thead>
        <tr>
          <th></th>
          <th>Filename</th>
          <th>Folder</th>
          <th>Date taken (EXIF)</th>
          <th>Date taken (file)</th>
          <th>Dimensions</th>
          <th>Size</th>
        </tr>
      </thead>
    `;
    const tbody = document.createElement('tbody');

    for (const g of meaningfulGroups) {
      const headerRow = document.createElement('tr');
      headerRow.className = 'group-header-row';
      const headerCell = document.createElement('td');
      headerCell.colSpan = 7;
      const total = g.total_members || g.files.length;
      const countLabel = g.truncated ? `${g.files.length} of ${total.toLocaleString()} shown` : `${total.toLocaleString()}`;
      if (groupBy === 'date') {
        headerCell.textContent = `Date: ${g.key} (${countLabel} photos)`;
      } else if (groupBy === 'filename') {
        headerCell.textContent = `Filename: ${g.key} (${countLabel} copies)`;
      } else {
        headerCell.textContent = `Duplicate group #${g.key} (${countLabel} copies)`;
      }
      if (g.truncated) {
        const notice = document.createElement('span');
        notice.className = 'truncated-notice';
        notice.textContent = ` — too many to list in full; showing the first ${g.files.length}`;
        headerCell.appendChild(notice);
      }
      headerRow.appendChild(headerCell);
      tbody.appendChild(headerRow);

      // The group's own file ids, in display order -- passed to the
      // full-size viewer so Left/Right there can cycle "previous, current,
      // next" within this same set of duplicates.
      const groupIds = g.files.map(f => f.id);
      for (const f of g.files) {
        tbody.appendChild(buildFileRow(f, groupIds));
      }
    }

    table.appendChild(tbody);
    groupsContainer.innerHTML = '';
    groupsContainer.appendChild(table);
  }

  function formatDate(datetimeStr) {
    // 'YYYY-MM-DD HH:MM:SS' -> 'YYYY-MM-DD HH:MM' (seconds rarely matter, keep it compact)
    if (!datetimeStr) return '';
    return datetimeStr.slice(0, 16);
  }

  function buildFileRow(f, groupIds) {
    const row = document.createElement('tr');
    row.className = 'file-row';
    row.title = 'Hover to preview above · double-click to open full size';

    const cbCell = document.createElement('td');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.fileId = f.id;
    cb.checked = !!f.marked_for_delete;
    cb.addEventListener('change', () => markFile(f.id, cb.checked));
    cb.addEventListener('click', (e) => e.stopPropagation());
    cb.addEventListener('dblclick', (e) => e.stopPropagation()); // don't open the viewer when double-clicking the checkbox
    cbCell.appendChild(cb);

    const nameCell = document.createElement('td');
    nameCell.className = 'rel-path-cell';
    nameCell.textContent = f.rel_path.split('/').pop();

    const folderCell = document.createElement('td');
    folderCell.className = 'rel-path-cell';
    folderCell.textContent = f.folder;

    const exifDateCell = document.createElement('td');
    exifDateCell.className = 'num-cell';
    exifDateCell.textContent = f.exif_datetime_raw ? formatDate(f.exif_datetime_raw) : '—';

    const fileDateCell = document.createElement('td');
    fileDateCell.className = 'num-cell';
    fileDateCell.textContent = formatDate(f.exif_datetime);

    const dimsCell = document.createElement('td');
    dimsCell.className = 'num-cell';
    dimsCell.textContent = (f.width && f.height) ? `${f.width}×${f.height}` : '';

    const sizeCell = document.createElement('td');
    sizeCell.className = 'num-cell';
    sizeCell.textContent = f.size ? `${(f.size / 1024).toLocaleString(undefined, {maximumFractionDigits: 0})} KB` : '';

    row.append(cbCell, nameCell, folderCell, exifDateCell, fileDateCell, dimsCell, sizeCell);

    row.addEventListener('mouseenter', () => showPreview(f));
    row.addEventListener('dblclick', () => {
      window.open(`/view/${f.id}?ids=${groupIds.join(',')}`, '_blank');
    });

    return row;
  }

  // ---- Scenario: hover panel shows current-under-cursor + the previously
  // hovered photo side by side, so it's easy to compare two candidates.
  function showPreview(f) {
    if (currentPreview) {
      previewPreviousImg.src = currentPreview.src;
      previewPreviousCaption.textContent = currentPreview.caption;
    }
    currentPreview = {src: `/api/thumb/${f.id}`, caption: f.rel_path};
    previewCurrentImg.src = currentPreview.src;
    previewCurrentCaption.textContent = currentPreview.caption;
  }

  async function markFile(fileId, marked) {
    await fetch('/api/mark', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file_id: fileId, marked_for_delete: marked})
    });
  }

  deleteBtn.addEventListener('click', async () => {
    const checked = [...document.querySelectorAll('#groups-container input[type=checkbox]:checked')];
    if (!checked.length) { reviewMsg.textContent = 'Nothing marked.'; return; }
    if (!confirm(`Move ${checked.length} file(s) to Trash?`)) return;
    const fileIds = checked.map(cb => Number(cb.dataset.fileId));
    reviewMsg.textContent = 'Moving to Trash...';
    const res = await fetch('/api/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file_ids: fileIds})
    });
    const data = await res.json();
    const failed = data.results.filter(r => !r.ok);
    reviewMsg.textContent = failed.length
      ? `Moved ${data.results.length - failed.length}, ${failed.length} failed.`
      : `Moved ${data.results.length} file(s) to Trash.`;
    loadGroups();
    loadSummary();
  });

  hideBtn.addEventListener('click', async () => {
    const checked = [...document.querySelectorAll('#groups-container input[type=checkbox]:checked')];
    if (!checked.length) { reviewMsg.textContent = 'Nothing marked.'; return; }
    const fileIds = checked.map(cb => Number(cb.dataset.fileId));
    reviewMsg.textContent = 'Hiding from results...';
    const res = await fetch('/api/hide', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file_ids: fileIds})
    });
    const data = await res.json();
    reviewMsg.textContent = data.ok
      ? `Hid ${data.hidden} file(s) from results. Nothing was deleted from disk.`
      : 'Error: ' + (data.error || 'unknown');
    loadGroups();
    loadSummary();
  });

  groupBySelect.addEventListener('change', resetToFirstPage);
  showReviewedToggle.addEventListener('change', resetToFirstPage);

  exportLink.addEventListener('click', (e) => {
    e.preventDefault();
    window.location.href = `/api/session/export?root_id=${rootId}&group_by=${groupBySelect.value}`;
  });

  importInput.addEventListener('change', async () => {
    if (!importInput.files.length) return;
    const formData = new FormData();
    formData.append('file', importInput.files[0]);
    reviewMsg.textContent = 'Loading session...';
    const res = await fetch('/api/session/import', {method: 'POST', body: formData});
    const data = await res.json();
    if (data.error) {
      reviewMsg.textContent = 'Error: ' + data.error;
      return;
    }
    reviewMsg.textContent = `Applied ${data.applied}, skipped ${data.skipped}.` +
      (data.warnings.length ? ` ${data.warnings.length} warning(s) — see console.` : '');
    if (data.warnings.length) console.warn('Session import warnings:', data.warnings);
    loadGroups();
  });

  restartPolling();
  loadGroups();
  loadSummary();
})();

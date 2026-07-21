// SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
//
// SPDX-License-Identifier: MPL-2.0

let errorContainer = document.getElementById('error-container');
let currentEntryId = null;
let activeAiBubble = null; // the <p> currently being filled by a streaming response
let currentDetailEntry = null; // the entry currently shown in the Notes detail view

const ui = new WebUI();
ui.on_connect(onUIConnected);
ui.on_disconnect(onUIDisconnected);

// --- Live camera preview ---
ui.on_message('frame', message => {
  const img = document.getElementById('cameraPreview');
  if (img) img.src = 'data:image/jpeg;base64,' + message.jpeg;
});

// --- Capture result: show the saved photo, start a fresh sketch/note for it ---
ui.on_message('photo_saved', message => {
  currentEntryId = message.id;

  const photoImg = document.getElementById('capturedPhoto');
  if (photoImg) {
    photoImg.src = 'data:image/jpeg;base64,' + message.image;
    photoImg.style.display = 'block';
  }

  clearSketchCanvas();
  const noteEl = document.getElementById('fieldNoteResult');
  if (noteEl) noteEl.innerHTML = '';

  setEntryStatus('photo', true);
  setEntryStatus('sketch', false);
  setEntryStatus('note', false);

  showStatus(`Photo saved as entry #${message.id}. Sketch it, then ask the local model about it below.`);

  // Move straight into the sketch/annotate step -- that's the natural
  // next action after a capture, and it's also what re-measures the
  // sketch canvas now that its tab is actually visible (see switchTab).
  switchTab('sketch');
});

ui.on_message('sketch_saved', message => {
  setEntryStatus('sketch', true);
  showStatus(`Sketch saved to entry #${message.id}.`);
});

ui.on_message('field_note', message => {
  const noteEl = document.getElementById('fieldNoteResult');
  if (noteEl) {
    const time = formatTimestamp(message.timestamp);
    noteEl.innerHTML = `
      <div class="field-note">
        <p>${escapeHtml(message.note)}</p>
        <span class="scan-content-time">Entry #${message.id} — ${time}</span>
      </div>
    `;
  }
  setEntryStatus('note', true);
  showStatus('Field note updated.');
});

ui.on_message('status', message => {
  showStatus(message.text);
});

// --- Chat with the local LLM (streamed) ---
ui.on_message('llm_response_chunk', chunk => {
  if (!activeAiBubble) {
    activeAiBubble = startAiBubble();
  }
  activeAiBubble.rawText += chunk;
  activeAiBubble.textEl.textContent = activeAiBubble.rawText;
  scrollChatToBottom();
});

ui.on_message('llm_response_end', () => {
  if (activeAiBubble) {
    finishAiBubble(activeAiBubble);
    activeAiBubble = null;
  }
  showStatus('Ready.');
});

ui.on_message('llm_error', message => {
  if (activeAiBubble) {
    finishAiBubble(activeAiBubble);
    activeAiBubble = null;
  }
  showStatus(`Local model error: ${message.error}`);
});

// --- Birdsong identification ---
ui.on_message('bird_result', message => {
  renderBirdResult(message);
});

// --- Saved Notes tab ---
ui.on_message('entries_list', message => {
  renderEntriesList(message.entries || []);
});

ui.on_message('entry_detail', message => {
  renderEntryDetail(message);
});

ui.on_message('note_updated', message => {
  if (currentDetailEntry && currentDetailEntry.id === message.id) {
    currentDetailEntry.note = message.note;
    const textEl = document.getElementById('detailNoteText');
    if (textEl) textEl.textContent = message.note || '(no note recorded for this entry)';
    const syncStatus = document.getElementById('detailSyncStatus');
    if (syncStatus) syncStatus.textContent = 'Note saved.';
  }
});

ui.on_message('entry_deleted', message => {
  // If the deleted entry was the one currently in progress on the
  // Capture/Sketch/Chat tabs, clear that state too -- otherwise a
  // stray sketch-save or note-append would silently target a row
  // that no longer exists.
  if (currentEntryId === message.id) {
    currentEntryId = null;
    setEntryStatus('photo', false);
    setEntryStatus('sketch', false);
    setEntryStatus('note', false);
    const photoImg = document.getElementById('capturedPhoto');
    if (photoImg) {
      photoImg.src = '';
      photoImg.style.display = 'none';
    }
    const noteEl = document.getElementById('fieldNoteResult');
    if (noteEl) noteEl.innerHTML = '';
  }
  currentDetailEntry = null;
  showNotesList();
  ui.send_message('list_entries');
});

ui.on_message('sync_result', message => {
  const text = message.success
    ? '✓ Synced to field-notes.netlify.app'
    : `⚠ ${message.error || 'Sync failed.'}`;
  const syncStatus = document.getElementById('detailSyncStatus');
  const syncBtn = document.getElementById('detailSyncButton');
  if (syncStatus && currentDetailEntry && currentDetailEntry.id === message.id) {
    syncStatus.textContent = text;
  } else {
    showStatus(text);
  }
  if (syncBtn && currentDetailEntry && currentDetailEntry.id === message.id) {
    syncBtn.disabled = false;
  }
});

// Start the application once layout has fully settled -- the sketch
// canvas needs real, final pixel dimensions before we size it, and
// measuring too early (before external CSS/fonts finish applying) can
// leave it at 0x0, which silently breaks drawing.
window.addEventListener('load', () => {
  initializeTabs();
  initializeCapture();
  initializeSketchpad();
  initializeNotes();
  initializeBirdsong();
  showStatus('Press "c" or tap Capture to start a new entry.');
});

function onUIConnected() {
  if (errorContainer) {
    errorContainer.style.display = 'none';
    errorContainer.textContent = '';
  }
}

function onUIDisconnected() {
  if (errorContainer) {
    errorContainer.textContent = 'Connection to the board lost. Please check the connection.';
    errorContainer.style.display = 'block';
  }
}

function showStatus(text) {
  const el = document.getElementById('captureStatus');
  if (el) el.textContent = text;
}

// --- Entry status indicators (visible confirmation of what's saved) ---
function setEntryStatus(part, saved) {
  const el = document.getElementById(`status-${part}`);
  if (!el) return;
  el.classList.toggle('status-saved', saved);
  el.textContent = (saved ? '✓ ' : '○ ') + el.dataset.label;
}

// --- Tabs ----------------------------------------------------------------
// Four tabs share one screen on the 5" display: Capture, Sketch, Chat,
// Notes. Only the active panel is displayed; the rest are display:none.
// That matters for the sketch canvas specifically -- a <canvas> inside a
// display:none panel measures as 0x0, so its real pixel size can only be
// set *after* its tab becomes visible, not once at page load.
function initializeTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === `tab-${name}`);
  });

  if (name === 'sketch') {
    // Wait a frame so the panel's display:flex has actually taken
    // effect before we measure it -- otherwise this still reads 0x0.
    requestAnimationFrame(() => {
      const canvas = document.getElementById('sketchCanvas');
      if (canvas) sizeCanvasToDisplay(canvas);
    });
  }

  if (name === 'notes') {
    showNotesList();
    ui.send_message('list_entries');
  }
}

// --- Capture -----------------------------------------------------------
function initializeCapture() {
  const captureButton = document.getElementById('captureButton');
  if (captureButton) captureButton.addEventListener('click', triggerCapture);

  const chatSendButton = document.getElementById('chatSendButton');
  if (chatSendButton) chatSendButton.addEventListener('click', sendChatMessage);

  const chatInput = document.getElementById('chatInput');
  if (chatInput) {
    chatInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') sendChatMessage();
    });
  }

  // Global, not tied to the Capture tab -- the whole point of moving off
  // evdev and onto this listener was that a 'c' press works no matter
  // what's on screen, so it should still fire from any tab.
  document.addEventListener('keydown', e => {
    const tag = e.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (e.key === 'c' || e.key === 'C') triggerCapture();
  });
}

function triggerCapture() {
  ui.send_message('capture');
  showStatus('Capturing photo...');
}

// --- Chat with the local LLM --------------------------------------------
function sendChatMessage() {
  const input = document.getElementById('chatInput');
  if (!input) return;
  const message = input.value.trim();
  if (!message) return;

  appendUserBubble(message);
  input.value = '';
  showStatus('Thinking...');
  ui.send_message('llm_chat', { message });
}

function appendUserBubble(text) {
  const log = document.getElementById('chatLog');
  if (!log) return;
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble chat-you';
  bubble.innerHTML = `<p>${escapeHtml(text)}</p>`;
  log.appendChild(bubble);
  scrollChatToBottom();
}

function startAiBubble() {
  const log = document.getElementById('chatLog');
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble chat-ai';
  const textEl = document.createElement('p');
  textEl.textContent = '';
  bubble.appendChild(textEl);
  if (log) log.appendChild(bubble);
  scrollChatToBottom();
  return { bubbleEl: bubble, textEl, rawText: '' };
}

function finishAiBubble(active) {
  if (!active.rawText.trim()) return;
  const addBtn = document.createElement('button');
  addBtn.className = 'add-to-note-btn';
  addBtn.textContent = '+ Add to Note';
  addBtn.addEventListener('click', () => addToNote(active.rawText.trim()));
  active.bubbleEl.appendChild(addBtn);
}

function scrollChatToBottom() {
  const log = document.getElementById('chatLog');
  if (log) log.scrollTop = log.scrollHeight;
}

function addToNote(text) {
  if (currentEntryId === null) {
    showStatus('Capture a photo first -- notes attach to an entry.');
    return;
  }
  ui.send_message('append_note', { entry_id: currentEntryId, text });
}

// --- Sketch pad --------------------------------------------------------
// Listens to BOTH Pointer Events (the standard, unifies touch/stylus/
// mouse) AND legacy touch events as a fallback, in case the browser
// running on the HDMI touchscreen doesn't fully support the Pointer
// Events spec. preventDefault() is called explicitly on every handler
// rather than relying solely on the touch-action CSS property, since
// some embedded WebViews ignore that CSS rule.
let sketchCtx = null;
let isDrawing = false;

function initializeSketchpad() {
  const canvas = document.getElementById('sketchCanvas');
  if (!canvas) return;

  sizeCanvasToDisplay(canvas);
  // Re-measure if the window/orientation changes.
  window.addEventListener('resize', () => sizeCanvasToDisplay(canvas));

  sketchCtx = canvas.getContext('2d');
  sketchCtx.lineWidth = 3;
  sketchCtx.lineCap = 'round';
  sketchCtx.lineJoin = 'round';
  sketchCtx.strokeStyle = '#1a1a1a';
  clearSketchCanvas();

  const startDraw = (x, y) => {
    isDrawing = true;
    sketchCtx.beginPath();
    sketchCtx.moveTo(x, y);
  };
  const moveDraw = (x, y) => {
    if (!isDrawing) return;
    sketchCtx.lineTo(x, y);
    sketchCtx.stroke();
  };
  const stopDraw = () => {
    isDrawing = false;
  };

  // Pointer Events (preferred path)
  canvas.addEventListener('pointerdown', e => {
    e.preventDefault();
    try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* not fatal */ }
    const pos = getCanvasPos(canvas, e.clientX, e.clientY);
    startDraw(pos.x, pos.y);
  });
  canvas.addEventListener('pointermove', e => {
    e.preventDefault();
    const pos = getCanvasPos(canvas, e.clientX, e.clientY);
    moveDraw(pos.x, pos.y);
  });
  canvas.addEventListener('pointerup', e => { e.preventDefault(); stopDraw(); });
  canvas.addEventListener('pointercancel', e => { e.preventDefault(); stopDraw(); });
  canvas.addEventListener('pointerleave', e => { e.preventDefault(); stopDraw(); });

  // Legacy touch event fallback
  canvas.addEventListener('touchstart', e => {
    e.preventDefault();
    const t = e.touches[0];
    if (!t) return;
    const pos = getCanvasPos(canvas, t.clientX, t.clientY);
    startDraw(pos.x, pos.y);
  }, { passive: false });
  canvas.addEventListener('touchmove', e => {
    e.preventDefault();
    const t = e.touches[0];
    if (!t) return;
    const pos = getCanvasPos(canvas, t.clientX, t.clientY);
    moveDraw(pos.x, pos.y);
  }, { passive: false });
  canvas.addEventListener('touchend', e => { e.preventDefault(); stopDraw(); }, { passive: false });
  canvas.addEventListener('touchcancel', e => { e.preventDefault(); stopDraw(); }, { passive: false });

  const clearButton = document.getElementById('sketchClearButton');
  if (clearButton) clearButton.addEventListener('click', clearSketchCanvas);

  const saveButton = document.getElementById('sketchSaveButton');
  if (saveButton) saveButton.addEventListener('click', saveSketch);
}

function sizeCanvasToDisplay(canvas) {
  const rect = canvas.getBoundingClientRect();
  // Fallback if layout hasn't settled to a real size yet for some reason
  // (e.g. the Sketch tab is still hidden -- see switchTab, which
  // re-measures once it isn't).
  const width = rect.width > 0 ? Math.round(rect.width) : 320;
  const height = rect.height > 0 ? Math.round(rect.height) : 240;
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    if (sketchCtx) clearSketchCanvas();
  }
}

function clearSketchCanvas() {
  const canvas = document.getElementById('sketchCanvas');
  if (!canvas || !sketchCtx) return;
  sketchCtx.fillStyle = '#ffffff';
  sketchCtx.fillRect(0, 0, canvas.width, canvas.height);
}

function getCanvasPos(canvas, clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  return { x: clientX - rect.left, y: clientY - rect.top };
}

function saveSketch() {
  if (currentEntryId === null) {
    showStatus('Capture a photo before saving a sketch.');
    return;
  }
  const canvas = document.getElementById('sketchCanvas');
  if (!canvas) return;
  const dataUrl = canvas.toDataURL('image/png');
  const base64 = dataUrl.split(',')[1];
  ui.send_message('sketch', { image: base64, entry_id: currentEntryId });
  showStatus('Saving sketch...');
}

// --- Birdsong identification --------------------------------------------
function initializeBirdsong() {
  const listenBtn = document.getElementById('birdListenButton');
  if (!listenBtn) return;

  listenBtn.addEventListener('click', () => {
    listenBtn.disabled = true;
    listenBtn.textContent = '🎤 Listening...';
    showStatus('Listening for birdsong...');
    const resultEl = document.getElementById('birdResult');
    if (resultEl) resultEl.innerHTML = '';
    ui.send_message('listen_for_birdsong');
  });
}

function renderBirdResult(message) {
  const listenBtn = document.getElementById('birdListenButton');
  if (listenBtn) {
    listenBtn.disabled = false;
    listenBtn.textContent = '🎤 Listen (5s)';
  }

  const el = document.getElementById('birdResult');
  if (!el) return;

  if (message.error) {
    el.innerHTML = `<p class="notes-empty">${escapeHtml(message.error)}</p>`;
    showStatus('Birdsong listen failed.');
    return;
  }

  if (!message.match) {
    el.innerHTML = '<p class="notes-empty">No confident match — try again, closer or when it\'s quieter.</p>';
    showStatus('No birdsong match.');
    return;
  }

  const pct = Math.round(message.confidence * 100);
  el.innerHTML = `
    <div class="field-note">
      <p>${escapeHtml(message.class_name)} — ${pct}% confidence</p>
      <button class="add-to-note-btn" id="addBirdToNoteBtn">+ Add to Note</button>
    </div>
  `;
  const addBtn = document.getElementById('addBirdToNoteBtn');
  if (addBtn) {
    addBtn.addEventListener('click', () => addToNote(`Heard: ${message.class_name} (${pct}% confidence)`));
  }
  showStatus('Birdsong identified.');
}

// --- Saved Notes: list + detail ------------------------------------------
function initializeNotes() {
  const backButton = document.getElementById('notesBackButton');
  if (backButton) backButton.addEventListener('click', showNotesList);
}

function renderEntriesList(entries) {
  const list = document.getElementById('entriesList');
  if (!list) return;

  if (!entries.length) {
    list.innerHTML = '<p class="notes-empty">No saved entries yet — capture a photo to start your first one.</p>';
    return;
  }

  list.innerHTML = entries.map(e => `
    <div class="entry-row" data-id="${e.id}">
      ${e.thumbnail
        ? `<img src="data:image/jpeg;base64,${e.thumbnail}" alt="">`
        : '<div class="entry-thumb-placeholder"></div>'}
      <div class="entry-meta">
        <div class="entry-time">${formatTimestamp(e.timestamp)}</div>
        <div class="entry-snippet">${e.note ? escapeHtml(e.note) : '(no note yet)'}</div>
      </div>
    </div>
  `).join('');

  list.querySelectorAll('.entry-row').forEach(row => {
    row.addEventListener('click', () => {
      ui.send_message('get_entry', { entry_id: Number(row.dataset.id) });
    });
  });
}

function renderEntryDetail(entry) {
  const content = document.getElementById('entryDetailContent');
  if (!content) return;

  currentDetailEntry = entry;

  content.innerHTML = `
    <div class="entry-time">${formatTimestamp(entry.timestamp)}</div>
    <div class="detail-images">
      ${entry.photo ? `<img src="data:image/jpeg;base64,${entry.photo}" alt="Photo">` : ''}
      ${entry.sketch ? `<img src="data:image/png;base64,${entry.sketch}" alt="Sketch">` : ''}
    </div>
    <div id="detailNoteView" class="field-note">
      <p id="detailNoteText">${entry.note ? escapeHtml(entry.note) : '(no note recorded for this entry)'}</p>
    </div>
    <div id="detailNoteEdit" class="field-note" style="display: none">
      <textarea id="detailNoteTextarea" rows="5">${entry.note ? escapeHtml(entry.note) : ''}</textarea>
    </div>
    <div class="detail-actions">
      <button id="detailEditButton">✏️ Edit</button>
      <button id="detailSaveButton" style="display: none">💾 Save</button>
      <button id="detailDeleteButton">🗑️ Delete</button>
      <button id="detailSyncButton">☁️ Sync to Cloud</button>
    </div>
    <div id="detailSyncStatus" class="capture-status"></div>
  `;

  attachDetailActionListeners(entry);
  showNotesDetail();
}

function attachDetailActionListeners(entry) {
  const editBtn = document.getElementById('detailEditButton');
  const saveBtn = document.getElementById('detailSaveButton');
  const deleteBtn = document.getElementById('detailDeleteButton');
  const syncBtn = document.getElementById('detailSyncButton');
  const viewEl = document.getElementById('detailNoteView');
  const editEl = document.getElementById('detailNoteEdit');
  const textarea = document.getElementById('detailNoteTextarea');
  const syncStatus = document.getElementById('detailSyncStatus');

  if (editBtn) {
    editBtn.addEventListener('click', () => {
      viewEl.style.display = 'none';
      editEl.style.display = 'block';
      editBtn.style.display = 'none';
      saveBtn.style.display = 'inline-block';
      textarea.focus();
    });
  }

  if (saveBtn) {
    saveBtn.addEventListener('click', () => {
      const text = textarea.value.trim();
      ui.send_message('update_note', { entry_id: entry.id, text });
      viewEl.style.display = 'block';
      editEl.style.display = 'none';
      editBtn.style.display = 'inline-block';
      saveBtn.style.display = 'none';
      if (syncStatus) syncStatus.textContent = 'Saving note...';
    });
  }

  if (deleteBtn) {
    deleteBtn.addEventListener('click', () => {
      const confirmed = window.confirm('Delete this field note? This cannot be undone.');
      if (!confirmed) return;
      ui.send_message('delete_entry', { entry_id: entry.id });
    });
  }

  if (syncBtn) {
    syncBtn.addEventListener('click', () => {
      syncBtn.disabled = true;
      if (syncStatus) syncStatus.textContent = 'Syncing to field-notes.netlify.app...';
      ui.send_message('sync_entry', { entry_id: entry.id });
    });
  }
}

function showNotesList() {
  const listView = document.getElementById('notesListView');
  const detailView = document.getElementById('notesDetailView');
  if (listView) listView.style.display = 'block';
  if (detailView) detailView.style.display = 'none';
}

function showNotesDetail() {
  const listView = document.getElementById('notesListView');
  const detailView = document.getElementById('notesDetailView');
  if (listView) listView.style.display = 'none';
  if (detailView) detailView.style.display = 'flex';
}

// --- Small helpers --------------------------------------------------------
const MONTH_ABBREVIATIONS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
];

// MMM/DD/YYYY, e.g. Jul/18/2026.
function formatTimestamp(ts) {
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return ts;
    const mm = MONTH_ABBREVIATIONS[d.getMonth()];
    const dd = String(d.getDate()).padStart(2, '0');
    const yyyy = d.getFullYear();
    return `${mm}/${dd}/${yyyy}`;
  } catch (e) {
    return ts;
  }
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

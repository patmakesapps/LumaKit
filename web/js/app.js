/**
 * LumaKit Web UI — main entry point.
 * Boots WebSocket, initializes components, routes views.
 */

import { WS } from './lib/ws.js';

// --- DOM refs ---
const $messages = document.getElementById('messages');
const $messagesInner = document.getElementById('messages-inner');
const $emptyState = document.getElementById('empty-state');
const $input = document.getElementById('input');
const $sendBtn = document.getElementById('send-btn');
const $chatList = document.getElementById('chat-list');
const $newChatBtn = document.getElementById('new-chat-btn');
const $topbarTitle = document.getElementById('topbar-title');
const $modelBadge = document.getElementById('model-badge');
const $statusDot = document.getElementById('status-dot');
const $sidebarToggle = document.getElementById('sidebar-toggle');
const $sidebar = document.getElementById('sidebar');
const $diffPanel = document.getElementById('diff-panel');
const $diffPanelBackdrop = document.getElementById('diff-panel-backdrop');
const $diffPanelTool = document.getElementById('diff-panel-tool');
const $diffPanelPath = document.getElementById('diff-panel-path');
const $diffPanelBody = document.getElementById('diff-panel-body');
const $diffPanelFooter = document.getElementById('diff-panel-footer');
const $diffPanelClose = document.getElementById('diff-panel-close');
const $diffPanelApprove = document.getElementById('diff-panel-approve');
const $diffPanelDeny = document.getElementById('diff-panel-deny');
const $navTasks = document.getElementById('nav-tasks');
const $navSettings = document.getElementById('nav-settings');
const $taskList = document.getElementById('task-list');
const $settingsContent = document.getElementById('settings-content');

// --- State ---
let isWorking = false;
let currentView = 'chat';
let currentChatId = null;
let statusEl = null;
// Pending confirm card awaiting a decision (only one at a time)
let pendingConfirm = null;
let currentTurnHadRichReply = false;

// --- Markdown setup ---
if (window.marked) {
    marked.setOptions({
        breaks: true,
        gfm: true,
        highlight: (code, lang) => {
            if (window.hljs && lang && hljs.getLanguage(lang)) {
                return hljs.highlight(code, { language: lang }).value;
            }
            return code;
        },
    });
}

// --- Helpers ---
function renderMarkdown(text) {
    if (!text) return '';
    if (window.marked) {
        return marked.parse(text);
    }
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\n/g, '<br>');
}

function scrollToBottom() {
    $messages.scrollTop = $messages.scrollHeight;
}

function setWorking(working) {
    isWorking = working;
    // Type /stop to interrupt — no UI toggle needed
}

function removeStatus() {
    if (statusEl) {
        statusEl.remove();
        statusEl = null;
    }
}

function exitCenteredMode() {
    const chatView = document.getElementById('chat-view');
    chatView.classList.remove('chat-view-centered');
}

function enterCenteredMode() {
    const chatView = document.getElementById('chat-view');
    chatView.classList.add('chat-view-centered');
}

function addMessage(role, content) {
    if ($emptyState && !$emptyState.classList.contains('hidden')) {
        $emptyState.classList.add('hidden');
        exitCenteredMode();
    }
    removeStatus();

    const div = document.createElement('div');
    div.className = `message ${role}`;
    div.dataset.role = role;

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.innerHTML = renderMarkdown(content);

    div.appendChild(bubble);
    $messagesInner.appendChild(div);
    scrollToBottom();

    // Highlight code blocks
    if (window.hljs) {
        div.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));
    }
}

function getLastUserMessage() {
    const messages = $messagesInner.querySelectorAll('.message.user');
    return messages.length ? messages[messages.length - 1] : null;
}

function addReactionToLatestUserMessage(emoji) {
    if (!emoji) return;
    const message = getLastUserMessage();
    if (!message) return;

    let reaction = message.querySelector('.message-reaction');
    if (!reaction) {
        reaction = document.createElement('div');
        reaction.className = 'message-reaction';
        message.appendChild(reaction);
    }

    reaction.textContent = emoji;
    scrollToBottom();
}

function addDeliveredImage(url, caption = '') {
    if (!url) return;
    if ($emptyState && !$emptyState.classList.contains('hidden')) {
        $emptyState.classList.add('hidden');
        exitCenteredMode();
    }
    removeStatus();

    const div = document.createElement('div');
    div.className = 'message assistant image-message';
    div.dataset.role = 'assistant';

    const bubble = document.createElement('div');
    bubble.className = 'bubble';

    const link = document.createElement('a');
    link.className = 'delivered-image-link';
    link.href = url;
    link.target = '_blank';
    link.rel = 'noreferrer noopener';

    const image = document.createElement('img');
    image.className = 'delivered-image';
    image.src = url;
    image.alt = caption || 'Delivered image';
    image.loading = 'lazy';

    link.appendChild(image);
    bubble.appendChild(link);

    if (caption) {
        const captionEl = document.createElement('div');
        captionEl.className = 'delivered-image-caption';
        captionEl.textContent = caption;
        bubble.appendChild(captionEl);
    }

    div.appendChild(bubble);
    $messagesInner.appendChild(div);
    scrollToBottom();
}

function parseToolMessageContent(content) {
    if (!content) return null;

    try {
        return JSON.parse(content);
    } catch {
        return null;
    }
}

function restoreRichToolMessage(message) {
    const parsed = parseToolMessageContent(message?.content);
    const data = parsed?.data || {};

    if (message?.name === 'react_to_message' && data.reacted && data.emoji) {
        addReactionToLatestUserMessage(data.emoji);
        return true;
    }

    if (
        ['send_photo_user', 'screenshot_user'].includes(message?.name) &&
        data.sent &&
        data.interface === 'web' &&
        data.url
    ) {
        addDeliveredImage(data.url, data.caption || '');
        return true;
    }

    return false;
}

function isInlineToolResult(data) {
    return !data.error && ['react_to_message', 'send_photo_user', 'screenshot_user'].includes(data.name);
}

function addToolCard(name, detail, isResult = false, isError = false) {
    removeStatus();

    const div = document.createElement('div');
    div.className = `tool-card${isResult ? ' result' : ''}${isError ? ' error' : ''}`;

    if (isResult) {
        div.innerHTML = `<span class="tool-detail">${detail}</span>`;
    } else {
        div.innerHTML = `<span class="tool-name">${name}</span><span class="tool-detail">${detail || ''}</span>`;
    }

    $messagesInner.appendChild(div);
    scrollToBottom();
}

function showStatus(text) {
    removeStatus();
    statusEl = document.createElement('div');
    statusEl.className = 'status-msg';
    statusEl.textContent = text;
    $messagesInner.appendChild(statusEl);
    scrollToBottom();
}

function clearMessages() {
    $messagesInner.innerHTML = '';
    $emptyState.classList.remove('hidden');
    enterCenteredMode();
    currentTurnHadRichReply = false;
}

// --- Views ---
function switchView(view) {
    currentView = view;
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById(`${view}-view`).classList.add('active');

    $navTasks.classList.toggle('active', view === 'task');
    $navSettings.classList.toggle('active', view === 'settings');

    if (view === 'task') loadTasks();
    if (view === 'settings') loadSettings();
}

// --- Chat list ---
async function loadChatList() {
    try {
        const res = await fetch('/api/chats');
        const chats = await res.json();
        $chatList.innerHTML = '';
        for (const chat of chats) {
            const item = document.createElement('div');
            item.className = `chat-item${chat.id === currentChatId ? ' active' : ''}`;
            item.textContent = chat.title || 'Untitled';
            item.onclick = () => {
                ws.send({ type: 'load_chat', chat_id: chat.id });
                $sidebar.classList.remove('open');
            };
            $chatList.appendChild(item);
        }
    } catch (e) {
        console.error('Failed to load chats:', e);
    }
}

// --- Tasks ---
async function loadTasks() {
    try {
        const res = await fetch('/api/tasks');
        const tasks = await res.json();
        if (tasks.length === 0) {
            $taskList.innerHTML = '<p style="color: var(--text-muted)">No tasks yet.</p>';
            return;
        }
        $taskList.innerHTML = '';
        for (const task of tasks) {
            const item = document.createElement('div');
            item.className = 'task-item';
            item.innerHTML = `
                <div class="task-title">${task.title}</div>
                <div class="task-meta">
                    <span class="task-status ${task.status}">${task.status}</span>
                    <span>${task.created_at?.slice(0, 16) || ''}</span>
                </div>
            `;
            $taskList.appendChild(item);
        }
    } catch (e) {
        $taskList.innerHTML = '<p style="color: var(--error)">Failed to load tasks.</p>';
    }
}

// --- Settings ---
async function loadSettings() {
    try {
        const res = await fetch('/api/settings');
        const settings = await res.json();
        $settingsContent.innerHTML = `
            <div class="setting-row">
                <span class="setting-label">Model</span>
                <span class="setting-value">${settings.model || 'not set'}</span>
            </div>
            <div class="setting-row">
                <span class="setting-label">Fallback Model</span>
                <span class="setting-value">${settings.fallback_model || 'none'}</span>
            </div>
            <div class="setting-row">
                <span class="setting-label">Data Directory</span>
                <span class="setting-value">${settings.data_dir}</span>
            </div>
        `;
    } catch (e) {
        $settingsContent.innerHTML = '<p style="color: var(--error)">Failed to load settings.</p>';
    }
}

// --- Health check for model badge ---
async function loadHealth() {
    try {
        const res = await fetch('/api/health');
        const data = await res.json();
        $modelBadge.textContent = data.model || 'unknown';
    } catch (e) {
        $modelBadge.textContent = 'offline';
    }
}

// --- WebSocket ---
const ws = new WS({
    onConnect() {
        $statusDot.classList.remove('disconnected');
        loadChatList();
        loadHealth();
    },

    onDisconnect() {
        $statusDot.classList.add('disconnected');
    },

    response(data) {
        setWorking(false);
        removeStatus();
        const text = (data.text || '').trim();
        if (text) {
            addMessage('assistant', text);
        } else if (!currentTurnHadRichReply) {
            // Agent finished but produced no text — surface a subtle marker so
            // the UI doesn't look frozen after a tool-only round.
            addMessage('assistant', '_Done._');
        }
        currentTurnHadRichReply = false;

        if (data.title) {
            $topbarTitle.textContent = data.title;
            currentChatId = data.chat_id;
            loadChatList();
        }
    },

    status(data) {
        showStatus(data.text);
    },

    tool_call(data) {
        addToolCard(data.name, data.detail);
    },

    tool_result(data) {
        if (isInlineToolResult(data)) return;
        const isError = Boolean(data.error);
        addToolCard('', data.summary, true, isError);
    },

    reaction(data) {
        addReactionToLatestUserMessage(data.emoji);
        currentTurnHadRichReply = true;
    },

    image(data) {
        addDeliveredImage(data.url, data.caption || '');
        currentTurnHadRichReply = true;
    },

    error(data) {
        setWorking(false);
        removeStatus();
        currentTurnHadRichReply = false;
        addMessage('assistant', `Error: ${data.text}`);
    },

    confirm(data) {
        showConfirmCard(data);
    },

    chat_loaded(data) {
        currentChatId = data.chat_id;
        $topbarTitle.textContent = data.title || 'New Chat';
        clearMessages();

        const hasMessages = (data.messages || []).some(m => m.role === 'user' || m.role === 'assistant');
        if (hasMessages) exitCenteredMode();

        for (const msg of data.messages || []) {
            if (msg.role === 'system') continue;
            if (msg.role === 'tool') {
                restoreRichToolMessage(msg);
                continue;
            }
            if (msg.role === 'user' || msg.role === 'assistant') {
                const content = msg.content || '';
                if (content) addMessage(msg.role, content);
            }
        }

        loadChatList();
        switchView('chat');
    },
});

// --- Inline confirm card + right-side diff panel ---

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function renderDiffLines(diffText) {
    const lines = diffText.split('\n');
    const out = [];
    for (const line of lines) {
        // Skip the unified-diff file headers — the panel header already shows the path
        if (line.startsWith('--- ') || line.startsWith('+++ ')) continue;
        let cls = 'diff-line';
        if (line.startsWith('@@')) cls += ' diff-hunk';
        else if (line.startsWith('+')) cls += ' diff-add';
        else if (line.startsWith('-')) cls += ' diff-del';
        out.push(`<div class="${cls}">${escapeHtml(line) || '&nbsp;'}</div>`);
    }
    return out.join('');
}

function openDiffPanel(data) {
    $diffPanelTool.textContent = data.tool_name || 'diff';
    $diffPanelPath.textContent = data.path || data.detail || '';
    $diffPanelBody.innerHTML = renderDiffLines(data.diff || '');
    // Panel carries its own approve/deny buttons only while a decision is pending
    $diffPanelFooter.classList.toggle('hidden', !pendingConfirm);
    $diffPanel.classList.remove('hidden');
    $diffPanelBackdrop.classList.remove('hidden');
    $diffPanelBody.scrollTop = 0;
}

function closeDiffPanel() {
    $diffPanel.classList.add('hidden');
    $diffPanelBackdrop.classList.add('hidden');
}

function resolveConfirmCard(approved) {
    if (!pendingConfirm) return;
    const { card, data } = pendingConfirm;
    card.classList.add('resolved');
    const status = card.querySelector('.confirm-card-status');
    if (status) {
        status.classList.add(approved ? 'approved' : 'denied');
        status.textContent = approved ? '\u2713 Approved' : '\u2717 Denied';
    }
    ws.send({ type: 'confirm_response', approved });
    pendingConfirm = null;
    closeDiffPanel();
}

function showConfirmCard(data) {
    removeStatus();
    if ($emptyState && !$emptyState.classList.contains('hidden')) {
        $emptyState.classList.add('hidden');
        exitCenteredMode();
    }

    // The tool_call card emitted just before the confirm is redundant with the
    // richer confirm card we're about to render — fold them into one.
    const last = $messagesInner.lastElementChild;
    if (last && last.classList.contains('tool-card') && !last.classList.contains('result')) {
        last.remove();
    }

    const toolName = data.tool_name || 'action';
    const detail = data.detail || '';
    const prompt = data.prompt || 'Approve this action?';
    const hasDiff = !!(data.diff && data.diff.trim());

    const card = document.createElement('div');
    card.className = 'confirm-card';
    card.innerHTML = `
        <div class="confirm-card-head">
            <span class="confirm-card-icon">\u2713</span>
            <span class="confirm-card-tool">${escapeHtml(toolName)}</span>
            <span class="confirm-card-prompt">${escapeHtml(prompt)}</span>
        </div>
        <div class="confirm-card-detail">${escapeHtml(detail)}</div>
        <div class="confirm-card-actions">
            ${hasDiff ? '<button class="confirm-card-diff-link">View diff \u2192</button>' : ''}
            <span class="confirm-card-hint"><kbd>Y</kbd> approve &middot; <kbd>N</kbd> deny</span>
            <button class="confirm-btn confirm-no">Deny (N)</button>
            <button class="confirm-btn confirm-yes">Approve (Y)</button>
        </div>
        <div class="confirm-card-status"></div>
    `;

    $messagesInner.appendChild(card);
    scrollToBottom();

    pendingConfirm = { card, data };

    // Pull focus off the composer so Y/N keystrokes land here, not in the textarea
    if (document.activeElement === $input) $input.blur();

    card.querySelector('.confirm-yes').onclick = () => resolveConfirmCard(true);
    card.querySelector('.confirm-no').onclick = () => resolveConfirmCard(false);
    if (hasDiff) {
        card.querySelector('.confirm-card-diff-link').onclick = () => openDiffPanel(data);
        // Auto-open the panel so the user can see the diff immediately
        openDiffPanel(data);
    }
}

$diffPanelClose.onclick = closeDiffPanel;
$diffPanelBackdrop.onclick = closeDiffPanel;
$diffPanelApprove.onclick = () => resolveConfirmCard(true);
$diffPanelDeny.onclick = () => resolveConfirmCard(false);

document.addEventListener('keydown', (e) => {
    // Don't intercept while the user is typing in the composer
    const typing = document.activeElement === $input;

    if (pendingConfirm && !typing) {
        const k = e.key.toLowerCase();
        if (k === 'y' || k === 'enter') {
            e.preventDefault();
            resolveConfirmCard(true);
            return;
        }
        if (k === 'n') {
            e.preventDefault();
            resolveConfirmCard(false);
            return;
        }
    }

    if (e.key === 'Escape') {
        if (!$diffPanel.classList.contains('hidden')) {
            closeDiffPanel();
        }
    }
});

// --- Send message ---
// Users can send multiple messages in a row without waiting for a response.
function sendMessage() {
    const text = $input.value.trim();
    if (!text) return;

    currentTurnHadRichReply = false;
    addMessage('user', text);
    ws.send({ type: 'message', text });
    $input.value = '';
    $input.style.height = 'auto';
    setWorking(true);
}

$sendBtn.onclick = sendMessage;

$input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

// Auto-resize textarea
$input.addEventListener('input', () => {
    $input.style.height = 'auto';
    $input.style.height = Math.min($input.scrollHeight, 200) + 'px';
});

// New chat
$newChatBtn.onclick = () => {
    ws.send({ type: 'new_chat' });
    $sidebar.classList.remove('open');
};

// Sidebar toggle (mobile)
$sidebarToggle.onclick = () => {
    $sidebar.classList.toggle('open');
};

// Navigation
$navTasks.onclick = () => switchView('task');
$navSettings.onclick = () => switchView('settings');

// Click outside sidebar to close on mobile
document.addEventListener('click', (e) => {
    if (window.innerWidth <= 768 &&
        $sidebar.classList.contains('open') &&
        !$sidebar.contains(e.target) &&
        e.target !== $sidebarToggle) {
        $sidebar.classList.remove('open');
    }
});

// --- Boot ---
enterCenteredMode();
ws.connect();
$input.focus();

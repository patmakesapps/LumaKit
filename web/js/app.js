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
const $stopBtn = document.getElementById('stop-btn');
const $chatList = document.getElementById('chat-list');
const $newChatBtn = document.getElementById('new-chat-btn');
const $topbarTitle = document.getElementById('topbar-title');
const $modelBadge = document.getElementById('model-badge');
const $statusDot = document.getElementById('status-dot');
const $sidebarToggle = document.getElementById('sidebar-toggle');
const $sidebar = document.getElementById('sidebar');
const $confirmModal = document.getElementById('confirm-modal');
const $confirmPrompt = document.getElementById('confirm-prompt');
const $confirmYes = document.getElementById('confirm-yes');
const $confirmNo = document.getElementById('confirm-no');
const $navTasks = document.getElementById('nav-tasks');
const $navSettings = document.getElementById('nav-settings');
const $taskList = document.getElementById('task-list');
const $settingsContent = document.getElementById('settings-content');

// --- State ---
let isWorking = false;
let currentView = 'chat';
let currentChatId = null;
let statusEl = null;

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
    $sendBtn.classList.toggle('hidden', working);
    $stopBtn.classList.toggle('hidden', !working);
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
        addMessage('assistant', data.text);

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
        const isError = data.summary?.includes('error') || false;
        addToolCard('', data.summary, true, isError);
    },

    error(data) {
        setWorking(false);
        removeStatus();
        addMessage('assistant', `Error: ${data.text}`);
    },

    confirm(data) {
        $confirmPrompt.textContent = data.prompt;
        $confirmModal.classList.remove('hidden');
    },

    chat_loaded(data) {
        currentChatId = data.chat_id;
        $topbarTitle.textContent = data.title || 'New Chat';
        clearMessages();

        const hasMessages = (data.messages || []).some(m => m.role === 'user' || m.role === 'assistant');
        if (hasMessages) exitCenteredMode();

        for (const msg of data.messages || []) {
            if (msg.role === 'system') continue;
            if (msg.role === 'tool') continue;
            if (msg.role === 'user' || msg.role === 'assistant') {
                const content = msg.content || '';
                if (content) addMessage(msg.role, content);
            }
        }

        loadChatList();
        switchView('chat');
    },
});

// --- Confirm modal handlers ---
$confirmYes.onclick = () => {
    ws.send({ type: 'confirm_response', approved: true });
    $confirmModal.classList.add('hidden');
};

$confirmNo.onclick = () => {
    ws.send({ type: 'confirm_response', approved: false });
    $confirmModal.classList.add('hidden');
};

// --- Send message ---
// Users can send multiple messages in a row without waiting for a response.
function sendMessage() {
    const text = $input.value.trim();
    if (!text) return;

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

// Stop button
$stopBtn.onclick = () => {
    ws.send({ type: 'stop' });
    setWorking(false);
    removeStatus();
};

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

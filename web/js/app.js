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
const $setupOverlay = document.getElementById('setup-overlay');
const $setupOpenSettings = document.getElementById('setup-open-settings');

// --- State ---
let isWorking = false;
let currentView = 'chat';
let currentChatId = null;
let statusEl = null;
let activityCardEl = null;
let activityTitleEl = null;
let activityLiveEl = null;
let activityListEl = null;
let activityLastText = '';
// Pending confirm card awaiting a decision (only one at a time)
let pendingConfirm = null;
let currentTurnHadRichReply = false;
let notificationPollTimer = null;
const emailDraftCards = new Map();
let settingsState = null;
let requiresModelSetup = false;

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

function applySetupState() {
    const blocked = !!requiresModelSetup;
    $input.disabled = blocked;
    $sendBtn.disabled = blocked;
    $newChatBtn.disabled = blocked;
    if (blocked) {
        $input.placeholder = 'Choose a model in Settings before chatting...';
        $setupOverlay.classList.remove('hidden');
        if (currentView !== 'settings') {
            switchView('settings');
        }
    } else {
        $input.placeholder = 'Message Lumi... (type /stop to interrupt)';
        $setupOverlay.classList.add('hidden');
    }
}

function removeStatus() {
    if (statusEl) {
        statusEl.remove();
        statusEl = null;
    }
}

function clearActivityCard() {
    if (activityCardEl) {
        activityCardEl.remove();
        activityCardEl = null;
        activityTitleEl = null;
        activityLiveEl = null;
        activityListEl = null;
        activityLastText = '';
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

function ensureActivityCard() {
    if (activityCardEl) return activityCardEl;
    if ($emptyState && !$emptyState.classList.contains('hidden')) {
        $emptyState.classList.add('hidden');
        exitCenteredMode();
    }
    removeStatus();

    const div = document.createElement('div');
    div.className = 'message assistant activity-message';
    div.dataset.role = 'assistant';

    const bubble = document.createElement('div');
    bubble.className = 'bubble activity-bubble';
    bubble.innerHTML = `
        <div class="activity-header">
            <span class="activity-dot"></span>
            <span class="activity-title">Lumi is thinking</span>
        </div>
        <div class="activity-live">Working through it...</div>
        <div class="activity-log"></div>
    `;

    div.appendChild(bubble);
    $messagesInner.appendChild(div);
    activityCardEl = div;
    activityTitleEl = bubble.querySelector('.activity-title');
    activityLiveEl = bubble.querySelector('.activity-live');
    activityListEl = bubble.querySelector('.activity-log');
    scrollToBottom();
    return div;
}

function _normalizedForDedupe(text) {
    return String(text || '')
        .toLowerCase()
        .replace(/[\s.…:;!?]+$/g, '')
        .replace(/\s+/g, ' ')
        .trim();
}

function appendActivityLine(text, kind = 'status') {
    const value = String(text || '').trim();
    if (!value) return;
    ensureActivityCard();
    const normalized = _normalizedForDedupe(value);
    const lastNormalized = _normalizedForDedupe(activityLastText);
    // Drop exact repeats AND substring overlaps so things like
    // "browser_automation for https://x" immediately followed by
    // "navigating to https://x" don't show as two lines.
    if (normalized && lastNormalized) {
        if (
            normalized === lastNormalized ||
            normalized.includes(lastNormalized) ||
            lastNormalized.includes(normalized)
        ) {
            // Keep the richer line visible.
            const richer = value.length >= activityLastText.length ? value : activityLastText;
            if (activityLiveEl) activityLiveEl.textContent = richer;
            if (richer !== activityLastText && activityListEl.lastElementChild) {
                activityListEl.lastElementChild.textContent = richer;
                activityLastText = richer;
            }
            return;
        }
    }

    const line = document.createElement('div');
    line.className = `activity-line ${kind}`;
    line.textContent = value;
    activityListEl.appendChild(line);
    activityLastText = value;

    while (activityListEl.children.length > 8) {
        activityListEl.removeChild(activityListEl.firstChild);
    }
    if (activityLiveEl) activityLiveEl.textContent = value;
    scrollToBottom();
}

function setActivityHeadline(text) {
    ensureActivityCard();
    if (activityTitleEl) activityTitleEl.textContent = text;
    if (activityLiveEl) activityLiveEl.textContent = text;
    scrollToBottom();
}

function settleActivityCard(state = 'done') {
    if (!activityCardEl) return;
    activityCardEl.classList.remove('active', 'done', 'error', 'stopped');
    activityCardEl.classList.add(state);
    if (activityTitleEl) {
        activityTitleEl.textContent =
            state === 'error' ? 'Lumi hit a problem'
            : state === 'stopped' ? 'Lumi stopped'
            : 'What Lumi did';
    }
}

function formatPlainText(text) {
    return escapeHtml(String(text || '')).replace(/\n/g, '<br>');
}

function renderEmailSections(email, { includeDraft = true } = {}) {
    const bodyPreview = email?.body_preview || '';
    const summary = email?.summary || '';
    const draftPreview = includeDraft ? (email?.draft_preview || '') : '';
    const links = Array.isArray(email?.links) ? email.links : [];

    const sections = [];

    if (bodyPreview) {
        sections.push(`
            <div class="email-card-section">
                <div class="email-card-section-title">Body</div>
                <div class="email-card-section-body">${formatPlainText(bodyPreview)}</div>
            </div>
        `);
    }

    if (summary) {
        sections.push(`
            <div class="email-card-section">
                <div class="email-card-section-title">Lumi's Take</div>
                <div class="email-card-section-body">${formatPlainText(summary)}</div>
            </div>
        `);
    }

    if (draftPreview) {
        sections.push(`
            <div class="email-card-section">
                <div class="email-card-section-title">Draft Reply</div>
                <div class="email-card-section-body">${formatPlainText(draftPreview)}</div>
            </div>
        `);
    }

    if (links.length) {
        const linkItems = links.map(link => `
            <li class="email-card-link-item">
                <span class="email-card-link-url">${escapeHtml(link.url || '')}</span>
                <span class="email-card-link-label">${escapeHtml(link.label || '')}</span>
            </li>
        `).join('');
        sections.push(`
            <div class="email-card-section">
                <div class="email-card-section-title">Links Lumi Could Not See</div>
                <ul class="email-card-links">${linkItems}</ul>
            </div>
        `);
    }

    return sections.join('');
}

function renderIncomingEmailCard(email) {
    return `
        <div class="email-card">
            <div class="email-card-head">
                <span class="email-card-badge">New Email</span>
                <span class="email-card-subtitle">Background notification</span>
            </div>
            <div class="email-card-meta">
                <div class="email-card-row">
                    <span class="email-card-key">From</span>
                    <span class="email-card-value">${escapeHtml(email?.from || '')}</span>
                </div>
                <div class="email-card-row">
                    <span class="email-card-key">Subject</span>
                    <span class="email-card-value">${escapeHtml(email?.subject || '(no subject)')}</span>
                </div>
            </div>
            ${renderEmailSections(email)}
        </div>
    `;
}

function renderEmailConfirmCard(data) {
    const preview = data.email_preview || {};
    const actionLabel = preview.action === 'reply' ? 'Reply Preview' : 'Email Preview';
    const remaining = preview.remaining_after_send;
    return `
        <div class="confirm-card email-confirm-card">
            <div class="confirm-card-head">
                <span class="confirm-card-icon">✉</span>
                <span class="confirm-card-tool">${escapeHtml(data.tool_name || 'email')}</span>
                <span class="confirm-card-prompt">${escapeHtml(data.prompt || 'Approve this email?')}</span>
            </div>
            <div class="email-card">
                <div class="email-card-head">
                    <span class="email-card-badge">${escapeHtml(actionLabel)}</span>
                    <span class="email-card-subtitle">Review before sending</span>
                </div>
                <div class="email-card-meta">
                    <div class="email-card-row">
                        <span class="email-card-key">To</span>
                        <span class="email-card-value">${escapeHtml(preview.to || '')}</span>
                    </div>
                    ${preview.cc ? `
                        <div class="email-card-row">
                            <span class="email-card-key">CC</span>
                            <span class="email-card-value">${escapeHtml(preview.cc)}</span>
                        </div>
                    ` : ''}
                    <div class="email-card-row">
                        <span class="email-card-key">Subject</span>
                        <span class="email-card-value">${escapeHtml(preview.subject || '(no subject)')}</span>
                    </div>
                    ${typeof remaining === 'number' ? `
                        <div class="email-card-row">
                            <span class="email-card-key">Remaining</span>
                            <span class="email-card-value">${escapeHtml(String(remaining))} send(s) after approval</span>
                        </div>
                    ` : ''}
                </div>
                <div class="email-card-section">
                    <div class="email-card-section-title">Message</div>
                    <div class="email-card-section-body">${formatPlainText(preview.body || '')}</div>
                </div>
            </div>
            <div class="confirm-card-actions">
                <span class="confirm-card-hint"><kbd>Y</kbd> approve &middot; <kbd>N</kbd> deny</span>
                <button class="confirm-btn confirm-no">Deny (N)</button>
                <button class="confirm-btn confirm-yes">Approve (Y)</button>
            </div>
            <div class="confirm-card-status"></div>
        </div>
    `;
}

function setEmailDraftPendingState(draftId, pendingText) {
    const entry = emailDraftCards.get(String(draftId));
    if (!entry) return;
    entry.approve.disabled = true;
    entry.discard.disabled = true;
    entry.status.textContent = pendingText;
    entry.status.classList.remove('approved', 'denied');
    entry.status.classList.add('pending');
}

function resolveEmailDraftCard(data) {
    const draftId = data.draft_id != null ? String(data.draft_id) : null;
    const entry = draftId ? emailDraftCards.get(draftId) : null;
    if (!entry) {
        addMessage('assistant', `${data.ok ? '✓' : '✗'} ${data.text}`);
        return;
    }

    entry.actions.remove();
    entry.status.textContent = `${data.ok ? '✓' : '✗'} ${data.text}`;
    entry.status.classList.remove('pending');
    entry.status.classList.add(data.ok ? 'approved' : 'denied');
    emailDraftCards.delete(draftId);
    scrollToBottom();
}

function addBackgroundMessage(data) {
    const text = (data.text || '').trim();
    if (!text) return;

    const draftId = data.draft_id != null ? String(data.draft_id) : null;
    if (draftId && emailDraftCards.has(draftId)) {
        return;
    }

    if ($emptyState && !$emptyState.classList.contains('hidden')) {
        $emptyState.classList.add('hidden');
        exitCenteredMode();
    }
    removeStatus();

    const div = document.createElement('div');
    div.className = 'message assistant';
    div.dataset.role = 'assistant';

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    if (data.kind === 'email' && data.email) {
        div.classList.add('email-card-message');
        bubble.innerHTML = renderIncomingEmailCard(data.email);
    } else {
        bubble.innerHTML = renderMarkdown(text);
    }
    div.appendChild(bubble);

    if (draftId) {
        const actionParent = data.kind === 'email' && data.email
            ? bubble.querySelector('.email-card') || bubble
            : bubble;
        const actions = document.createElement('div');
        actions.className = 'email-draft-actions';

        const discard = document.createElement('button');
        discard.className = 'confirm-btn confirm-no';
        discard.textContent = 'Discard';
        discard.onclick = () => {
            setEmailDraftPendingState(draftId, 'Discarding...');
            ws.send({ type: 'email_draft_action', action: 'discard', draft_id: Number(draftId) });
        };

        const approve = document.createElement('button');
        approve.className = 'confirm-btn confirm-yes';
        approve.textContent = 'Send';
        approve.onclick = () => {
            setEmailDraftPendingState(draftId, 'Sending...');
            ws.send({ type: 'email_draft_action', action: 'approve', draft_id: Number(draftId) });
        };

        const status = document.createElement('div');
        status.className = 'email-draft-status';

        actions.appendChild(discard);
        actions.appendChild(approve);
        actionParent.appendChild(actions);
        actionParent.appendChild(status);
        emailDraftCards.set(draftId, { approve, discard, actions, status });
    }

    $messagesInner.appendChild(div);
    scrollToBottom();

    if (window.hljs) {
        div.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));
    }
}

function getLastUserMessage() {
    const messages = $messagesInner.querySelectorAll('.message.user');
    return messages.length ? messages[messages.length - 1] : null;
}

function markLastUserMessageQueued(text) {
    // Find the user bubble that matches the queued text (most recent first) —
    // matching by text is more robust than "last" when several queued messages
    // are in flight.
    const target = (text || '').trim();
    const messages = $messagesInner.querySelectorAll('.message.user');
    let match = null;
    for (let i = messages.length - 1; i >= 0; i--) {
        const bubbleText = messages[i].querySelector('.bubble')?.innerText?.trim() || '';
        if (!target || bubbleText === target) {
            match = messages[i];
            break;
        }
    }
    if (!match) return;
    if (match.querySelector('.queued-badge')) return;
    const badge = document.createElement('div');
    badge.className = 'queued-badge';
    badge.textContent = 'Queued — will apply after the current step';
    match.appendChild(badge);
    scrollToBottom();
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
    clearActivityCard();
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

            const title = document.createElement('span');
            title.className = 'chat-item-title';
            title.textContent = chat.title || 'Untitled';
            title.onclick = () => {
                ws.send({ type: 'load_chat', chat_id: chat.id });
                $sidebar.classList.remove('open');
            };

            const del = document.createElement('button');
            del.className = 'chat-item-delete';
            del.title = 'Delete chat';
            del.setAttribute('aria-label', 'Delete chat');
            del.innerHTML = '&times;';
            del.onclick = async (e) => {
                e.stopPropagation();
                if (!confirm(`Delete "${chat.title || 'Untitled'}"? This can't be undone.`)) return;
                try {
                    await fetch(`/api/chats/${encodeURIComponent(chat.id)}`, { method: 'DELETE' });
                    if (chat.id === currentChatId) {
                        window.location.reload();
                        return;
                    }
                    loadChatList();
                } catch (err) {
                    console.error('Failed to delete chat:', err);
                }
            };

            item.appendChild(title);
            item.appendChild(del);
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
        if (!res.ok) {
            throw new Error(`Settings API returned ${res.status}`);
        }
        const settings = await res.json();
        settingsState = settings;
        requiresModelSetup = !!settings.setup_required;

        const modelOptions = (settings.installed_models || [])
            .map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
            .join('');
        const banner = settings.setup_required ? `
            <div class="settings-banner">
                No primary model is configured yet. Pick one below before using the web UI.
            </div>
        ` : '';
        const modelsError = settings.installed_models_error
            ? `<div class="settings-error">Could not load installed models from Ollama: ${escapeHtml(settings.installed_models_error)}</div>`
            : '';

        $settingsContent.innerHTML = `
            ${banner}
            <div class="settings-card">
                <h3>Runtime Models</h3>
                <div class="settings-note">
                    Choose the model LumaKit should use by default in the web UI. These settings persist in the app data directory and override the .env defaults until you reset them.
                </div>
                <form id="settings-form" class="settings-form">
                    <div class="settings-field">
                        <label for="primary-model-input">Primary Model</label>
                        <input id="primary-model-input" class="settings-input" type="text" value="${escapeHtml(settings.app_primary_model || settings.model || '')}" placeholder="e.g. glm-5:cloud or qwen3">
                    </div>
                    <div class="settings-field">
                        <label for="fallback-model-input">Fallback Model</label>
                        <input id="fallback-model-input" class="settings-input" type="text" value="${escapeHtml(settings.app_fallback_model || settings.fallback_model || '')}" placeholder="Optional">
                    </div>
                    <div class="settings-field">
                        <label for="installed-models-select">Detected Ollama Models</label>
                        <select id="installed-models-select" class="settings-select">
                            <option value="">Choose an installed model...</option>
                            ${modelOptions}
                        </select>
                    </div>
                    ${modelsError}
                    <div class="settings-actions">
                        <button type="submit" class="settings-btn primary">Save Models</button>
                        <button type="button" id="reset-model-settings" class="settings-btn secondary">Reset To .env Defaults</button>
                    </div>
                </form>
            </div>
            <div class="setting-row">
                <span class="setting-label">Effective Primary Model</span>
                <span class="setting-value">${settings.model || 'not set'}</span>
            </div>
            <div class="setting-row">
                <span class="setting-label">Effective Fallback Model</span>
                <span class="setting-value">${settings.fallback_model || 'none'}</span>
            </div>
            <div class="setting-row">
                <span class="setting-label">.env Primary Model</span>
                <span class="setting-value">${settings.env_primary_model || 'not set'}</span>
            </div>
            <div class="setting-row">
                <span class="setting-label">.env Fallback Model</span>
                <span class="setting-value">${settings.env_fallback_model || 'none'}</span>
            </div>
            <div class="setting-row">
                <span class="setting-label">Optional Local Model</span>
                <span class="setting-value">${settings.local_model || 'not set'}</span>
            </div>
            <div class="setting-row">
                <span class="setting-label">Data Directory</span>
                <span class="setting-value">${settings.data_dir}</span>
            </div>
        `;

        const $primaryModelInput = document.getElementById('primary-model-input');
        const $fallbackModelInput = document.getElementById('fallback-model-input');
        const $installedModelsSelect = document.getElementById('installed-models-select');
        const $settingsForm = document.getElementById('settings-form');
        const $resetModelSettings = document.getElementById('reset-model-settings');

        $installedModelsSelect?.addEventListener('change', () => {
            if ($installedModelsSelect.value) {
                $primaryModelInput.value = $installedModelsSelect.value;
            }
        });

        $settingsForm?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const primary_model = $primaryModelInput.value.trim();
            const fallback_model = $fallbackModelInput.value.trim();
            if (!primary_model && !(settings.env_primary_model || '').trim()) {
                loadSettingsError('Choose a primary model or set OLLAMA_MODEL in .env first.');
                return;
            }
            await saveSettings({ primary_model, fallback_model });
        });

        $resetModelSettings?.addEventListener('click', async () => {
            await saveSettings({ primary_model: '', fallback_model: '' });
        });

        applySetupState();
    } catch (e) {
        console.error('Failed to load settings', e);
        const message = e?.message || String(e);
        $settingsContent.innerHTML = `<p style="color: var(--error)">Failed to load settings: ${escapeHtml(message)}</p>`;
    }
}

function loadSettingsError(message) {
    const existing = $settingsContent.querySelector('.settings-error-inline');
    if (existing) existing.remove();
    const error = document.createElement('p');
    error.className = 'settings-error settings-error-inline';
    error.textContent = message;
    $settingsContent.prepend(error);
}

async function saveSettings(payload) {
    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error('save failed');
        await loadSettings();
        await loadHealth();
    } catch (e) {
        loadSettingsError('Failed to save settings.');
    }
}

// --- Health check for model badge ---
async function loadHealth() {
    try {
        const res = await fetch('/api/health');
        const data = await res.json();
        $modelBadge.textContent = data.model || 'unknown';
        if (typeof data.setup_required === 'boolean') {
            requiresModelSetup = data.setup_required;
            applySetupState();
        }
    } catch (e) {
        $modelBadge.textContent = 'offline';
    }
}

async function pollNotifications() {
    if (!ws.connected) return;
    try {
        const res = await fetch('/api/notifications/unshown');
        const notifications = await res.json();
        for (const item of notifications) {
            const handler = ws.handlers[item.type];
            if (handler) handler(item);
        }
    } catch (e) {
        console.error('Failed to poll notifications:', e);
    }
}

function startNotificationPolling() {
    if (notificationPollTimer) return;
    notificationPollTimer = setInterval(() => {
        pollNotifications();
    }, 5000);
    pollNotifications();
}

function stopNotificationPolling() {
    if (!notificationPollTimer) return;
    clearInterval(notificationPollTimer);
    notificationPollTimer = null;
}

// --- WebSocket ---
const ws = new WS({
    onConnect() {
        $statusDot.classList.remove('disconnected');
        loadChatList();
        loadHealth();
        startNotificationPolling();
    },

    onDisconnect() {
        $statusDot.classList.add('disconnected');
        stopNotificationPolling();
    },

    response(data) {
        setWorking(false);
        const runState = data.run_state || 'completed';
        const cardState = runState === 'failed' ? 'error'
            : runState === 'stopped' || runState === 'interrupted' ? 'stopped'
            : 'done';
        settleActivityCard(cardState);
        removeStatus();
        const text = (data.text || '').trim();
        const runError = (data.run_error || '').trim();
        if (text) {
            addMessage('assistant', text);
        } else if (runState === 'failed' && runError) {
            addMessage('assistant', `_Run stopped: ${runError}_`);
        } else if (runState === 'failed') {
            addMessage('assistant', '_Run stopped before a reply was produced._');
        } else if (!currentTurnHadRichReply) {
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
        const text = String(data.text || '').trim();
        if (!text) return;
        if (text === 'Lumi is thinking...' || text === 'Lumi is thinking') {
            setActivityHeadline('Lumi is thinking');
            return;
        }
        if (text === 'Lumi is working...' || text === 'Lumi is working') {
            setActivityHeadline('Lumi is working');
            return;
        }
        if (text === 'Stopping...') {
            appendActivityLine('Stopping...', 'status');
            settleActivityCard('stopped');
            return;
        }
        appendActivityLine(text, 'status');
    },

    tool_call(data) {
        const detail = data.detail ? `: ${data.detail}` : '';
        appendActivityLine(`Using ${data.name}${detail}`, 'tool');
    },

    tool_result(data) {
        if (isInlineToolResult(data)) return;
        appendActivityLine(data.summary, data.error ? 'error' : 'result');
    },

    reaction(data) {
        addReactionToLatestUserMessage(data.emoji);
        currentTurnHadRichReply = true;
    },

    image(data) {
        addDeliveredImage(data.url, data.caption || '');
        currentTurnHadRichReply = true;
    },

    message(data) {
        addBackgroundMessage(data);
    },

    message_queued(data) {
        markLastUserMessageQueued(data.text || '');
    },

    reminder(data) {
        const label = data.label || 'Reminder';
        addMessage('assistant', `🔔 ${label}: ${data.text}`);
    },

    email_draft_result(data) {
        resolveEmailDraftCard(data);
    },

    error(data) {
        setWorking(false);
        settleActivityCard('error');
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
    const isEmailConfirm = data.kind === 'email' && data.email_preview;

    const card = document.createElement('div');
    if (isEmailConfirm) {
        // handled below via a detached wrapper so we can bind actions normally
    } else {
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
    }

    const renderedCard = isEmailConfirm ? (() => {
        const wrapper = document.createElement('div');
        wrapper.innerHTML = renderEmailConfirmCard(data);
        return wrapper.firstElementChild;
    })() : card;

    $messagesInner.appendChild(renderedCard);
    scrollToBottom();

    pendingConfirm = { card: renderedCard, data };

    // Pull focus off the composer so Y/N keystrokes land here, not in the textarea
    if (document.activeElement === $input) $input.blur();

    renderedCard.querySelector('.confirm-yes').onclick = () => resolveConfirmCard(true);
    renderedCard.querySelector('.confirm-no').onclick = () => resolveConfirmCard(false);
    if (hasDiff && !isEmailConfirm) {
        renderedCard.querySelector('.confirm-card-diff-link').onclick = () => openDiffPanel(data);
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
    if (requiresModelSetup) {
        switchView('settings');
        return;
    }

    // Only reset the activity card when starting a fresh turn — if the agent
    // is still working, the user's new message is queued alongside the
    // existing activity, not starting a new one.
    if (!isWorking) {
        currentTurnHadRichReply = false;
        clearActivityCard();
    }
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
$setupOpenSettings.onclick = () => switchView('settings');

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
loadSettings();
$input.focus();

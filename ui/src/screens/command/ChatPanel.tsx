import { useEffect, useRef, useState } from 'react';
import type { ChatMessage } from '../../api/types';
import { useI18n, type MessageKey } from '../../i18n';
import { textDir } from '../../lib/format';
import type { ScreenId } from '../../lib/router';
import { useApi, useAppState, useDispatch, useRunner } from '../../state/context';
import { useSendCommand, useStickToBottom } from '../../state/hooks';
import type { PendingChat } from '../../state/types';
import { Icon } from '../../components/Icon';
import { Markdown } from '../../components/Markdown';
import { Logo } from '../../components/shell/Shell';
import { ConfirmButton, EmptyState, Panel } from '../../components/ui';
import { BidiText } from '../../components/BidiText';

const KIND_ICONS: Record<string, string> = { ack: 'check', plan: 'list', result: 'check-circle', error: 'alert', info: 'info' };

function MessageItem({ message, onOpenTask }: { message: ChatMessage; onOpenTask: (id: string) => void }) {
  const { t } = useI18n();
  const dir = textDir(message.text, message.language);
  if (message.role === 'system') {
    return (
      <li className="msg msg--system" data-testid="chat-message" data-role="system">
        <span className="msg__system bidi">{message.text}</span>
      </li>
    );
  }
  const jarvis = message.role === 'jarvis';
  const showKind = jarvis && message.kind !== 'answer';
  return (
    <li className={`msg msg--${message.role} msg--${message.kind}`} data-testid="chat-message" data-role={message.role} data-kind={message.kind}>
      {jarvis ? (
        <span className="msg__avatar" aria-hidden="true">
          <Logo size={22} />
        </span>
      ) : null}
      <div className="msg__bubble">
        {showKind ? (
          <span className="msg__kind">
            <Icon name={KIND_ICONS[message.kind] ?? 'info'} size={12} />
            {t(`chat.kind.${message.kind}` as MessageKey)}
          </span>
        ) : null}
        {message.kind === 'ack' ? (
          <p className="msg__text bidi" dir={dir}>
            <BidiText text={message.text} />
          </p>
        ) : (
          <Markdown text={message.text} className="msg__text" dir={dir} />
        )}
        <div className="msg__meta">
          <span className="num ltr">{message.time}</span>
          {message.source === 'voice' ? (
            <span className="msg__src" title={t('chat.voice')}>
              <Icon name="mic" size={11} />
            </span>
          ) : null}
          {message.task_id ? (
            <button type="button" className="msg__task" onClick={() => onOpenTask(message.task_id!)}>
              <Icon name="tasks" size={11} />
              {t('chat.openTask')}
            </button>
          ) : null}
        </div>
      </div>
    </li>
  );
}

function PendingItem({ pending, onRetry }: { pending: PendingChat; onRetry: () => void }) {
  const { t } = useI18n();
  return (
    <li className={`msg msg--user msg--pending ${pending.failed ? 'msg--failed' : ''}`} data-testid="chat-pending">
      <div className="msg__bubble">
        <p className="msg__text bidi" dir={textDir(pending.text)}>
          {pending.text}
        </p>
        <div className="msg__meta">
          {pending.failed ? (
            <>
              <span className="msg__failed">
                <Icon name="alert" size={11} />
                {t('chat.failed')}
              </span>
              <button type="button" className="msg__task" onClick={onRetry}>
                <Icon name="restart" size={11} />
                {t('common.retry')}
              </button>
            </>
          ) : (
            <span className="msg__sending">
              <span className="spinner spinner--xs" />
              {t('chat.sending')}
            </span>
          )}
        </div>
      </div>
    </li>
  );
}

/** Conversation with JARVIS: Enter sends, Shift+Enter adds a line. */
export function ChatPanel({ className, onNavigate }: { className?: string; onNavigate: (screen: ScreenId, param?: string | null) => void }) {
  const { t, dir: uiDir } = useI18n();
  const api = useApi();
  const dispatch = useDispatch();
  const chat = useAppState((s) => s.chat);
  const pending = useAppState((s) => s.pendingChat);
  const send = useSendCommand();
  const [text, setText] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const { run } = useRunner(t('common.requestFailed'));
  const lastId = chat.length ? chat[chat.length - 1].id : '';
  const { ref, onScroll, scrollToBottom } = useStickToBottom<HTMLOListElement>([lastId, pending.length], false);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(120, el.scrollHeight)}px`;
  }, [text]);

  const submit = () => {
    const value = text.trim();
    if (!value) return;
    setText('');
    scrollToBottom();
    void send(value);
  };

  const retry = (p: PendingChat) => {
    dispatch({ type: 'chat.pendingDone', clientId: p.clientId, failed: false });
    void send(p.text);
  };

  const clear = () =>
    void run('clear', async () => {
      await api.clearChat();
      dispatch({ type: 'chat.cleared' });
    });

  return (
    <Panel
      className={`chat-panel ${className ?? ''}`}
      title={t('chat.title')}
      icon="send"
      actions={
        chat.length ? (
          <ConfirmButton className="icon-btn icon-btn--sm" confirmLabel={t('common.confirm')} onConfirm={clear} title={t('chat.clear')} testId="chat-clear">
            <Icon name="trash" size={14} />
          </ConfirmButton>
        ) : null
      }
      testId="panel-chat"
      flush
    >
      <div className="chat">
        {chat.length || pending.length ? (
          <ol className="chat__list" ref={ref} onScroll={onScroll} aria-live="polite" data-testid="chat-list">
            {chat.map((message) => (
              <MessageItem key={message.id} message={message} onOpenTask={(id) => onNavigate('tasks', id)} />
            ))}
            {pending.map((p) => (
              <PendingItem key={p.clientId} pending={p} onRetry={() => retry(p)} />
            ))}
          </ol>
        ) : (
          <EmptyState icon="send" title={t('chat.empty')} hint={t('chat.emptyHint')} />
        )}
        <form
          className="chat__composer"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <textarea
            ref={inputRef}
            className="chat__input"
            rows={1}
            value={text}
            dir={text.trim() ? textDir(text) : uiDir}
            placeholder={t('chat.placeholder')}
            aria-label={t('chat.inputLabel')}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
            data-testid="chat-input"
          />
          <button type="submit" className="chat__send" disabled={!text.trim()} aria-label={t('chat.send')} title={t('chat.send')} data-testid="chat-send">
            <Icon name="send" size={17} />
          </button>
        </form>
      </div>
    </Panel>
  );
}

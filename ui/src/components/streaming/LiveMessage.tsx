import { useState, type ReactNode } from 'react';

interface UserMessageProps {
  text: string;
  timestamp: number;
}

export function UserMessage({ text }: UserMessageProps) {
  return (
    <div style={{
      marginBottom: '16px',
    }}>
      <div style={{
        fontSize: '10px',
        color: '#7C3AED',
        marginBottom: '6px',
        fontWeight: 700,
        textTransform: 'uppercase' as const,
        letterSpacing: '0.5px',
      }}>
        User
      </div>
      <div style={{
        fontSize: '14px',
        color: 'var(--text-primary)',
        lineHeight: '1.6',
      }}>
        {text}
      </div>
    </div>
  );
}

interface ToolCallMessageProps {
  name: string;
  args: Record<string, any>;
  timestamp: number;
}

function formatArgValue(value: any): string {
  if (typeof value === 'string') {
    if (value.length > 80) return `"${value.slice(0, 77)}…"`;
    return `"${value}"`;
  }
  const s = JSON.stringify(value);
  if (s.length > 80) return s.slice(0, 77) + '…';
  return s;
}

export function ToolCallMessage({ name, args }: ToolCallMessageProps) {
  const argsStr = Object.keys(args).length > 0
    ? Object.keys(args).map(k => `${k}=${formatArgValue(args[k])}`).join(', ')
    : '';

  return (
    <div style={{
      marginBottom: '2px',
      paddingLeft: '12px',
    }}>
      <div style={{
        fontSize: '12px',
        color: '#A855F7',
        fontFamily: 'monospace',
        fontWeight: 500,
        wordBreak: 'break-word',
      }}>
        → {name}({argsStr})
      </div>
    </div>
  );
}

const TRUNCATE_LINES = 6;

interface ToolResultMessageProps {
  response: Record<string, any>;
  isError?: boolean;
  timestamp: number;
  toolName?: string;
}

function extractDisplayText(response: Record<string, any>): string {
  if (typeof response === 'string') return response;
  if (response.stdout) return response.stdout;
  if (response.result && typeof response.result === 'string') return response.result;
  if (response.content && typeof response.content === 'string') return response.content;
  if (response.filePath && response.success !== undefined) {
    return `${response.filePath} ${response.success ? '(success)' : '(failed)'}`;
  }
  return JSON.stringify(response, null, 2);
}

export function ToolResultMessage({ response, isError }: ToolResultMessageProps) {
  const [expanded, setExpanded] = useState(false);
  const displayText = extractDisplayText(response);
  const lines = displayText.split('\n');
  const needsTruncation = lines.length > TRUNCATE_LINES;
  const visibleText = !expanded && needsTruncation
    ? lines.slice(0, TRUNCATE_LINES).join('\n') + `\n… +${lines.length - TRUNCATE_LINES} more lines`
    : displayText;

  const color = isError ? '#ef4444' : '#10b981';

  return (
    <div style={{
      marginBottom: '12px',
      paddingLeft: '12px',
    }}>
      <div
        style={{
          fontSize: '11px',
          color,
          fontFamily: 'monospace',
          fontWeight: 400,
          cursor: needsTruncation ? 'pointer' : 'default',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          lineHeight: '1.5',
        }}
        onClick={needsTruncation ? () => setExpanded(e => !e) : undefined}
        title={needsTruncation ? (expanded ? 'Click to collapse' : 'Click to expand') : undefined}
      >
        ← {visibleText}
      </div>
    </div>
  );
}

interface AgentMessageProps {
  text: string;
  timestamp: number;
  isStreaming?: boolean;
}

const codeBlockStyle: React.CSSProperties = {
  fontFamily: 'monospace',
  fontSize: '12px',
  background: 'rgba(0, 0, 0, 0.15)',
  borderRadius: '6px',
  padding: '12px',
  margin: '8px 0',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  lineHeight: '1.5',
  overflowX: 'auto',
};

const inlineCodeStyle: React.CSSProperties = {
  fontFamily: 'monospace',
  fontSize: '12px',
  background: 'rgba(0, 0, 0, 0.1)',
  borderRadius: '3px',
  padding: '1px 4px',
};

const tableStyle: React.CSSProperties = {
  borderCollapse: 'collapse',
  fontSize: '13px',
  margin: '8px 0',
  width: '100%',
};

const thStyle: React.CSSProperties = {
  borderBottom: '2px solid var(--border-default, #333)',
  padding: '6px 12px',
  textAlign: 'left',
  fontWeight: 600,
  whiteSpace: 'nowrap',
};

const tdStyle: React.CSSProperties = {
  borderBottom: '1px solid var(--border-default, #333)',
  padding: '4px 12px',
};

function parseTable(lines: string[], keyPrefix: string): ReactNode {
  const parseRow = (line: string) =>
    line.split('|').slice(1, -1).map(c => c.trim());

  const headers = parseRow(lines[0]);
  const rows = lines.slice(2).filter(l => l.trim()).map(parseRow);

  return (
    <table key={keyPrefix} style={tableStyle}>
      <thead>
        <tr>
          {headers.map((h, i) => (
            <th key={i} style={thStyle}>{renderInlineMarkdown(h, 0)}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, ri) => (
          <tr key={ri}>
            {row.map((cell, ci) => (
              <td key={ci} style={tdStyle}>{renderInlineMarkdown(cell, ri)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function renderSimpleMarkdown(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const lines = text.split('\n');
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.startsWith('```')) {
      const endIdx = lines.findIndex((l, j) => j > i && l.startsWith('```'));
      const codeLines = endIdx > i
        ? lines.slice(i + 1, endIdx)
        : lines.slice(i + 1);
      parts.push(
        <pre key={`cb-${parts.length}`} style={codeBlockStyle}>
          {codeLines.join('\n').trim()}
        </pre>
      );
      i = endIdx > i ? endIdx + 1 : lines.length;
      continue;
    }

    if (line.trim().startsWith('|') && i + 2 < lines.length
        && lines[i + 1].trim().match(/^\|[\s:|-]+\|$/)) {
      const tableLines: string[] = [line];
      let j = i + 1;
      while (j < lines.length && lines[j].trim().startsWith('|')) {
        tableLines.push(lines[j]);
        j++;
      }
      parts.push(parseTable(tableLines, `tbl-${parts.length}`));
      i = j;
      continue;
    }

    parts.push(...renderInlineMarkdown(line + (i < lines.length - 1 ? '\n' : ''), parts.length));
    i++;
  }

  return parts;
}

function renderInlineMarkdown(text: string, keyOffset: number): ReactNode[] {
  const parts: ReactNode[] = [];
  const inlineRegex = /`([^`]+)`|\*\*(.+?)\*\*/g;
  let lastIndex = 0;
  let match;

  while ((match = inlineRegex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(<span key={`t-${keyOffset}-${parts.length}`}>{text.slice(lastIndex, match.index)}</span>);
    }
    if (match[1] !== undefined) {
      parts.push(
        <code key={`ic-${keyOffset}-${parts.length}`} style={inlineCodeStyle}>{match[1]}</code>
      );
    } else if (match[2] !== undefined) {
      parts.push(
        <strong key={`b-${keyOffset}-${parts.length}`}>{match[2]}</strong>
      );
    }
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    parts.push(<span key={`t-${keyOffset}-${parts.length}`}>{text.slice(lastIndex)}</span>);
  }

  return parts;
}

export function AgentMessage({ text, isStreaming }: AgentMessageProps) {
  return (
    <div style={{
      marginBottom: '16px',
    }}>
      <div style={{
        fontSize: '10px',
        color: '#10b981',
        marginBottom: '6px',
        fontWeight: 700,
        textTransform: 'uppercase' as const,
        letterSpacing: '0.5px',
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
      }}>
        Agent
        {isStreaming && <span style={{
          display: 'inline-block',
          width: '6px',
          height: '6px',
          borderRadius: '50%',
          background: '#10b981',
          animation: 'pulse 1.5s ease-in-out infinite',
        }} />}
      </div>
      <div style={{
        fontSize: '14px',
        color: 'var(--text-primary)',
        lineHeight: '1.6',
        wordBreak: 'break-word',
      }}>
        {text ? renderSimpleMarkdown(text) : <em style={{ color: 'var(--text-tertiary)' }}>Thinking...</em>}
      </div>
    </div>
  );
}

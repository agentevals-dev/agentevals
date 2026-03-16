import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { css } from '@emotion/react';
import { Drawer, Button, Modal, message } from 'antd';
import { Save } from 'lucide-react';
import { useTraceContext } from '../../context/TraceContext';
import { InvocationEditor } from '../builder/InvocationEditor';
import { RawJsonPreview } from './RawJsonPreview';
import { readFileAsText } from '../../lib/utils';
import { loadJaegerTraces } from '../../lib/trace-loader';
import { convertTracesToInvocations } from '../../lib/trace-converter';
import { parseTraceFileForEditing, buildEditMappings, applyEditsAndSerialize } from '../../lib/trace-patcher';
import type { Invocation, ParsedTraceFile, SpanEditMapping } from '../../lib/types';

interface TraceEditorDrawerProps {
  file: File;
  fileIndex: number;
  open: boolean;
  onClose: () => void;
}

interface TraceGroup {
  traceId: string;
  invocations: Invocation[];
}

export const TraceEditorDrawer: React.FC<TraceEditorDrawerProps> = ({ file, fileIndex, open, onClose }) => {
  const { state, actions } = useTraceContext();
  const [traceGroups, setTraceGroups] = useState<TraceGroup[]>([]);
  const [parsedFile, setParsedFile] = useState<ParsedTraceFile | null>(null);
  const [editMappings, setEditMappings] = useState<SpanEditMapping[]>([]);
  const [loading, setLoading] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;

    setLoading(true);
    setError(null);
    setDirty(false);

    readFileAsText(file)
      .then(async (content) => {
        const parsed = parseTraceFileForEditing(content, file.name);
        setParsedFile(parsed);

        const traces = await loadJaegerTraces(content);
        const conversionResults = convertTracesToInvocations(traces);
        const mappings = buildEditMappings(traces, parsed);
        setEditMappings(mappings);

        const groups: TraceGroup[] = [];
        for (const trace of traces) {
          const result = conversionResults.get(trace.traceId);
          if (result && result.invocations.length > 0) {
            groups.push({ traceId: trace.traceId, invocations: result.invocations });
          }
        }
        setTraceGroups(groups);
        setLoading(false);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : 'Failed to parse trace file');
        setLoading(false);
      });
  }, [file, open]);

  const handleClose = useCallback(() => {
    if (dirty) {
      Modal.confirm({
        title: 'Discard changes?',
        content: 'You have unsaved changes that will be lost.',
        okText: 'Discard',
        okType: 'danger',
        onOk: onClose,
      });
    } else {
      onClose();
    }
  }, [dirty, onClose]);

  const handleApply = useCallback(() => {
    if (!parsedFile) return;

    const allInvocations = traceGroups.flatMap(g => g.invocations);
    const content = applyEditsAndSerialize(parsedFile, allInvocations, editMappings);
    const newFile = new File([content], file.name, { type: 'application/octet-stream' });

    const newFiles = [...state.traceFiles];
    newFiles[fileIndex] = newFile;
    actions.setTraceFiles(newFiles);

    setDirty(false);
    message.success('Trace file updated');
    onClose();
  }, [parsedFile, traceGroups, editMappings, file.name, fileIndex, state.traceFiles, actions, onClose]);

  const handleInvocationChange = useCallback((traceIdx: number, invIdx: number, updated: Invocation) => {
    setTraceGroups(prev => {
      const newGroups = [...prev];
      const group = { ...newGroups[traceIdx] };
      const invs = [...group.invocations];
      invs[invIdx] = updated;
      group.invocations = invs;
      newGroups[traceIdx] = group;
      return newGroups;
    });
    setDirty(true);
  }, []);

  const previewContent = useMemo(() => {
    if (!parsedFile || !dirty) return null;
    const allInvocations = traceGroups.flatMap(g => g.invocations);
    return applyEditsAndSerialize(parsedFile, allInvocations, editMappings);
  }, [parsedFile, traceGroups, editMappings, dirty]);

  const [initialContent, setInitialContent] = useState<string>('');
  useEffect(() => {
    if (open && parsedFile) {
      if (parsedFile.format === 'otlp-jsonl') {
        setInitialContent(parsedFile.rawData.map((line: any) => JSON.stringify(line)).join('\n'));
      } else {
        setInitialContent(JSON.stringify(parsedFile.rawData, null, 2));
      }
    }
  }, [open, parsedFile]);

  const totalInvocations = traceGroups.reduce((sum, g) => sum + g.invocations.length, 0);

  return (
    <Drawer
      title={null}
      placement="right"
      width="85%"
      open={open}
      onClose={handleClose}
      destroyOnClose
      closable={false}
      styles={{
        header: { display: 'none' },
        body: { padding: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', background: 'var(--bg-primary)' },
      }}
    >
      <div css={headerStyle}>
        <div css={leftSectionStyle}>
          <div css={titleStyle}>
            <h1>Edit Trace</h1>
            <span css={subtitleStyle}>{file.name}</span>
          </div>
          {dirty && <span css={unsavedBadgeStyle}>unsaved changes</span>}
        </div>
        <div css={rightSectionStyle}>
          <Button css={cancelButtonStyle} onClick={handleClose}>
            Cancel
          </Button>
          <Button css={applyButtonStyle} type="primary" icon={<Save size={16} />} onClick={handleApply} disabled={!dirty || !parsedFile}>
            Apply Changes
          </Button>
        </div>
      </div>

      {loading && (
        <div css={centeredMessageStyle}>Loading trace data...</div>
      )}

      {error && (
        <div css={css`${centeredMessageStyle}; color: var(--status-failure);`}>{error}</div>
      )}

      {!loading && !error && (
        <div css={contentStyle}>
          <div css={editorPanelStyle}>
            {totalInvocations === 0 ? (
              <div css={emptyMessageStyle}>
                No invocations could be extracted from this trace file.
              </div>
            ) : (
              traceGroups.map((group, gIdx) => (
                <div key={group.traceId} style={{ marginBottom: gIdx < traceGroups.length - 1 ? 24 : 0 }}>
                  {traceGroups.length > 1 && (
                    <div css={traceIdBadgeStyle}>
                      Trace: {group.traceId.substring(0, 16)}...
                      <span css={traceIdCountStyle}>
                        ({group.invocations.length} invocation{group.invocations.length !== 1 ? 's' : ''})
                      </span>
                    </div>
                  )}
                  <div css={sectionLabelStyle}>
                    Invocations ({group.invocations.length})
                  </div>
                  {group.invocations.map((inv, iIdx) => (
                    <InvocationEditor
                      key={inv.invocationId}
                      invocation={inv}
                      onChange={(updated) => handleInvocationChange(gIdx, iIdx, updated)}
                    />
                  ))}
                </div>
              ))
            )}
          </div>
          <div css={previewPanelStyle}>
            <RawJsonPreview
              content={previewContent || initialContent}
              title="Trace File Preview"
            />
          </div>
        </div>
      )}
    </Drawer>
  );
};

const headerStyle = css`
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  border-bottom: 1px solid var(--border-default);
  background: var(--bg-surface);
  flex-shrink: 0;
`;

const leftSectionStyle = css`
  display: flex;
  align-items: center;
  gap: 16px;
`;

const titleStyle = css`
  display: flex;
  flex-direction: column;
  gap: 4px;

  h1 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
  }
`;

const subtitleStyle = css`
  font-size: 0.75rem;
  color: var(--text-secondary);
  font-family: monospace;
`;

const unsavedBadgeStyle = css`
  font-size: 0.7rem;
  color: var(--status-warning);
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.1);
`;

const rightSectionStyle = css`
  display: flex;
  gap: 12px;
`;

const cancelButtonStyle = css`
  &.ant-btn {
    background: transparent;
    border-color: var(--border-default);
    color: var(--text-secondary);

    &:hover {
      border-color: var(--text-secondary);
      color: var(--text-primary);
      background: rgba(255, 255, 255, 0.04);
    }
  }
`;

const applyButtonStyle = css`
  &.ant-btn-primary {
    background-color: var(--accent-primary);
    border-color: var(--accent-primary);

    &:hover {
      background-color: var(--accent-primary);
      border-color: var(--accent-primary);
      opacity: 0.85;
    }

    &:disabled {
      background-color: rgba(168, 85, 247, 0.3);
      border-color: transparent;
      color: rgba(255, 255, 255, 0.4);
    }
  }
`;

const contentStyle = css`
  flex: 1;
  display: flex;
  flex-direction: row;
  overflow: hidden;
`;

const editorPanelStyle = css`
  width: 50%;
  flex-shrink: 0;
  overflow-y: auto;
  padding: 24px;
`;

const previewPanelStyle = css`
  width: 50%;
  flex-shrink: 0;
  border-left: 1px solid var(--border-default);
  background: var(--bg-surface);
  overflow-y: auto;
`;

const centeredMessageStyle = css`
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
  padding: 48px;
  color: var(--text-secondary);
  font-size: 0.875rem;
`;

const emptyMessageStyle = css`
  text-align: center;
  padding: 40px;
  color: var(--text-secondary);
  font-size: 0.875rem;
`;

const traceIdBadgeStyle = css`
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 12px;
  padding: 6px 10px;
  background: var(--bg-surface);
  border-radius: 6px;
  border: 1px solid var(--border-default);
  font-family: monospace;
`;

const traceIdCountStyle = css`
  font-weight: 400;
  margin-left: 8px;
`;

const sectionLabelStyle = css`
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 12px;
`;

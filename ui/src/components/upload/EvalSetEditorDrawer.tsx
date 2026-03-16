import React, { useState, useEffect, useCallback } from 'react';
import { css } from '@emotion/react';
import { Drawer, Button, Modal, message } from 'antd';
import { Save } from 'lucide-react';
import { useTraceContext } from '../../context/TraceContext';
import { MetadataEditor } from '../builder/MetadataEditor';
import { EvalCasesList } from '../builder/EvalCasesList';
import { JsonPreview } from '../builder/JsonPreview';
import { readFileAsText, convertSnakeToCamel, convertCamelToSnake } from '../../lib/utils';
import type { EvalSet, EvalSetMetadata, EvalCase, Invocation } from '../../lib/types';

function parseEvalSetFromJson(raw: any): EvalSet {
  return {
    eval_set_id: raw.eval_set_id || '',
    name: raw.name || '',
    description: raw.description || '',
    eval_cases: (raw.eval_cases || []).map((ec: any) => ({
      eval_id: ec.eval_id || '',
      conversation: (ec.conversation || []).map((inv: any) => convertSnakeToCamel(inv) as Invocation),
    })),
  };
}

function serializeEvalSet(evalSet: EvalSet): string {
  const raw = {
    eval_set_id: evalSet.eval_set_id,
    name: evalSet.name,
    description: evalSet.description,
    eval_cases: evalSet.eval_cases.map(ec => ({
      eval_id: ec.eval_id,
      conversation: ec.conversation.map(inv => convertCamelToSnake(inv)),
    })),
  };
  return JSON.stringify(raw, null, 2);
}

interface EvalSetEditorDrawerProps {
  file: File;
  open: boolean;
  onClose: () => void;
}

export const EvalSetEditorDrawer: React.FC<EvalSetEditorDrawerProps> = ({ file, open, onClose }) => {
  const { actions } = useTraceContext();
  const [evalSet, setEvalSet] = useState<EvalSet | null>(null);
  const [loading, setLoading] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;

    setLoading(true);
    setError(null);
    setDirty(false);

    readFileAsText(file)
      .then((content) => {
        const parsed = JSON.parse(content);
        setEvalSet(parseEvalSetFromJson(parsed));
        setLoading(false);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : 'Failed to parse eval set file');
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
    if (!evalSet) return;

    const jsonString = serializeEvalSet(evalSet);
    const newFile = new File([jsonString], file.name, { type: 'application/json' });
    actions.setEvalSet(newFile);
    setDirty(false);
    message.success('Eval set updated');
    onClose();
  }, [evalSet, file.name, actions, onClose]);

  const updateMetadata = useCallback((updates: Partial<EvalSetMetadata>) => {
    setEvalSet(prev => prev ? { ...prev, ...updates } : null);
    setDirty(true);
  }, []);

  const updateCase = useCallback((idx: number, ec: EvalCase) => {
    setEvalSet(prev => {
      if (!prev) return null;
      const newCases = [...prev.eval_cases];
      newCases[idx] = ec;
      return { ...prev, eval_cases: newCases };
    });
    setDirty(true);
  }, []);

  const removeCase = useCallback((idx: number) => {
    setEvalSet(prev => {
      if (!prev) return null;
      return { ...prev, eval_cases: prev.eval_cases.filter((_, i) => i !== idx) };
    });
    setDirty(true);
  }, []);

  const addCase = useCallback(() => {
    setEvalSet(prev => {
      if (!prev) return null;
      const newCase: EvalCase = {
        eval_id: `case_${prev.eval_cases.length + 1}`,
        conversation: [],
      };
      return { ...prev, eval_cases: [...prev.eval_cases, newCase] };
    });
    setDirty(true);
  }, []);

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
            <h1>Edit Eval Set</h1>
            <span css={subtitleStyle}>{file.name}</span>
          </div>
          {dirty && <span css={unsavedBadgeStyle}>unsaved changes</span>}
        </div>
        <div css={rightSectionStyle}>
          <Button css={cancelButtonStyle} onClick={handleClose}>
            Cancel
          </Button>
          <Button css={applyButtonStyle} type="primary" icon={<Save size={16} />} onClick={handleApply} disabled={!dirty || !evalSet}>
            Apply Changes
          </Button>
        </div>
      </div>

      {loading && (
        <div css={centeredMessageStyle}>Loading eval set...</div>
      )}

      {error && (
        <div css={css`${centeredMessageStyle}; color: var(--status-failure);`}>{error}</div>
      )}

      {!loading && !error && evalSet && (
        <div css={contentStyle}>
          <div css={editorPanelStyle}>
            <MetadataEditor
              metadata={{
                eval_set_id: evalSet.eval_set_id,
                name: evalSet.name,
                description: evalSet.description,
              }}
              onChange={updateMetadata}
            />
            <EvalCasesList
              evalCases={evalSet.eval_cases}
              onUpdateCase={updateCase}
              onRemoveCase={removeCase}
              onAddCase={addCase}
            />
          </div>
          <div css={previewPanelStyle}>
            <JsonPreview evalSet={evalSet} />
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

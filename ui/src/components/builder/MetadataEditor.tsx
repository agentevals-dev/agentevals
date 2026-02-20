import React from 'react';
import { css } from '@emotion/react';
import { Input } from 'antd';
import type { EvalSetMetadata } from '../../lib/types';

interface MetadataEditorProps {
  metadata: EvalSetMetadata;
  onChange: (updates: Partial<EvalSetMetadata>) => void;
}

export const MetadataEditor: React.FC<MetadataEditorProps> = ({
  metadata,
  onChange,
}) => {
  return (
    <div css={containerStyle}>
      <h2>EvalSet Metadata</h2>

      <div css={fieldStyle}>
        <label>EvalSet ID</label>
        <Input
          value={metadata.eval_set_id}
          onChange={(e) => onChange({ eval_set_id: e.target.value })}
          placeholder="eval_set_id"
        />
      </div>

      <div css={fieldStyle}>
        <label>Name</label>
        <Input
          value={metadata.name}
          onChange={(e) => onChange({ name: e.target.value })}
          placeholder="EvalSet name"
        />
      </div>

      <div css={fieldStyle}>
        <label>Description</label>
        <Input.TextArea
          value={metadata.description}
          onChange={(e) => onChange({ description: e.target.value })}
          placeholder="Description"
          rows={3}
        />
      </div>
    </div>
  );
};

const containerStyle = css`
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 24px;

  h2 {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 20px 0;
  }
`;

const fieldStyle = css`
  margin-bottom: 16px;

  &:last-child {
    margin-bottom: 0;
  }

  label {
    display: block;
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--text-primary);
    margin-bottom: 8px;
  }
`;

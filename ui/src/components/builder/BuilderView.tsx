import React from 'react';
import { css } from '@emotion/react';
import { message } from 'antd';
import { useTraceContext } from '../../context/TraceContext';
import { BuilderHeader } from './BuilderHeader';
import { MetadataEditor } from './MetadataEditor';
import { EvalCasesList } from './EvalCasesList';
import { JsonPreview } from './JsonPreview';
import { TraceUploadZone } from './TraceUploadZone';
import { downloadEvalSet, validateEvalSet } from '../../lib/evalset-builder';

export const BuilderView: React.FC = () => {
  const { state, actions } = useTraceContext();

  const handleSave = () => {
    if (!state.builderEvalSet) return;

    const errors = validateEvalSet(state.builderEvalSet);
    if (errors.length > 0) {
      message.error(`Validation failed: ${errors.join(', ')}`);
      return;
    }

    downloadEvalSet(state.builderEvalSet);
    message.success('EvalSet saved successfully!');
  };

  const handleBack = () => {
    if (state.results.length > 0) {
      actions.setCurrentView('dashboard');
    } else {
      actions.setCurrentView('welcome');
    }
  };

  if (!state.builderEvalSet) {
    return <TraceUploadZone />;
  }

  return (
    <div css={builderViewStyle}>
      <BuilderHeader
        onBack={handleBack}
        onSave={handleSave}
        evalSetId={state.builderEvalSet.eval_set_id}
      />

      <div css={builderContentStyle}>
        <div css={editorPanelStyle}>
          <MetadataEditor
            metadata={{
              eval_set_id: state.builderEvalSet.eval_set_id,
              name: state.builderEvalSet.name,
              description: state.builderEvalSet.description,
            }}
            onChange={actions.updateEvalSetMetadata}
          />

          <EvalCasesList
            evalCases={state.builderEvalSet.eval_cases}
            onUpdateCase={actions.updateEvalCase}
            onRemoveCase={actions.removeEvalCase}
            onAddCase={() => {
              actions.addEvalCase({
                eval_id: `case_${state.builderEvalSet!.eval_cases.length + 1}`,
                conversation: [],
              });
            }}
          />
        </div>

        <div css={previewPanelStyle}>
          <JsonPreview evalSet={state.builderEvalSet} />
        </div>
      </div>
    </div>
  );
};

const builderViewStyle = css`
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-primary);
`;

const builderContentStyle = css`
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

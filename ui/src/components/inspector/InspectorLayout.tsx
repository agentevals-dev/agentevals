import React, { useState, useRef, useEffect } from 'react';
import { css } from '@emotion/react';

interface InspectorLayoutProps {
  leftPanel: React.ReactNode;
  rightPanel: React.ReactNode;
}

const MIN_PANEL_WIDTH = 300;
const MAX_PANEL_WIDTH = 1000;
const STORAGE_KEY = 'inspector-panel-widths-2-panel';

export const InspectorLayout: React.FC<InspectorLayoutProps> = ({
  leftPanel,
  rightPanel,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [leftWidth, setLeftWidth] = useState(() => {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const { left } = JSON.parse(stored);
      return left || 400;
    }
    return 400;
  });
  const [isDragging, setIsDragging] = useState(false);

  // Save to localStorage when width changes
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ left: leftWidth }));
  }, [leftWidth]);

  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!containerRef.current || !isDragging) return;

      const containerRect = containerRef.current.getBoundingClientRect();
      const newWidth = e.clientX - containerRect.left;
      const constrainedWidth = Math.max(
        MIN_PANEL_WIDTH,
        Math.min(MAX_PANEL_WIDTH, newWidth)
      );
      setLeftWidth(constrainedWidth);
    };

    const handleMouseUp = () => {
      setIsDragging(false);
    };

    if (isDragging) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleMouseUp);
      return () => {
        document.removeEventListener('mousemove', handleMouseMove);
        document.removeEventListener('mouseup', handleMouseUp);
      };
    }
  }, [isDragging]);

  return (
    <div
      ref={containerRef}
      css={layoutStyles(leftWidth)}
      className={isDragging ? 'dragging' : ''}
    >
      <div css={panelStyles} className="left-panel">
        {leftPanel}
      </div>

      <div
        css={dividerStyles}
        className="divider"
        onMouseDown={handleMouseDown}
      />

      <div css={panelStyles} className="right-panel">
        {rightPanel}
      </div>
    </div>
  );
};

const layoutStyles = (leftWidth: number) => css`
  display: grid;
  grid-template-columns: ${leftWidth}px 1px 1fr;
  height: calc(100% - 64px);
  overflow: hidden;

  &.dragging {
    user-select: none;
    cursor: col-resize;
  }
`;

const panelStyles = css`
  background: var(--bg-surface);
  overflow: hidden;
  display: flex;
  flex-direction: column;

  &.left-panel {
    border-right: 1px solid var(--border-default);
  }

  &.right-panel {
    border-left: 1px solid var(--border-default);
  }
`;

const dividerStyles = css`
  background: var(--border-default);
  cursor: col-resize;
  position: relative;
  transition: background 0.2s ease, width 0.2s ease;

  &::before {
    content: '';
    position: absolute;
    top: 0;
    bottom: 0;
    left: -4px;
    right: -4px;
  }

  &:hover {
    background: var(--accent-cyan);
    width: 3px;
  }

  &:active {
    background: var(--accent-cyan);
    width: 3px;
  }
`;

import React, { useState, useRef, useEffect } from 'react';
import { css } from '@emotion/react';

interface InspectorLayoutProps {
  leftPanel: React.ReactNode;
  centerPanel?: React.ReactNode;
  rightPanel: React.ReactNode;
  showCenterPanel: boolean;
}

const MIN_PANEL_WIDTH = 300;
const MAX_PANEL_WIDTH = 1000;
const STORAGE_KEY_2_PANEL = 'inspector-panel-widths-2-panel';
const STORAGE_KEY_3_PANEL = 'inspector-panel-widths-3-panel';

export const InspectorLayout: React.FC<InspectorLayoutProps> = ({
  leftPanel,
  centerPanel,
  rightPanel,
  showCenterPanel,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [leftWidth, setLeftWidth] = useState(() => {
    const storageKey = showCenterPanel ? STORAGE_KEY_3_PANEL : STORAGE_KEY_2_PANEL;
    const stored = localStorage.getItem(storageKey);
    if (stored) {
      const { left } = JSON.parse(stored);
      return left || (showCenterPanel ? 300 : 400);
    }
    return showCenterPanel ? 300 : 400;
  });
  const [centerWidth, setCenterWidth] = useState(() => {
    if (!showCenterPanel) return 0;
    const stored = localStorage.getItem(STORAGE_KEY_3_PANEL);
    if (stored) {
      const { center } = JSON.parse(stored);
      return center || 600;
    }
    return 600;
  });
  const [rightWidth, setRightWidth] = useState(() => {
    const storageKey = showCenterPanel ? STORAGE_KEY_3_PANEL : STORAGE_KEY_2_PANEL;
    const stored = localStorage.getItem(storageKey);
    if (stored) {
      const { right } = JSON.parse(stored);
      return right || (showCenterPanel ? 500 : 600);
    }
    return showCenterPanel ? 500 : 600;
  });

  const [isDraggingLeft, setIsDraggingLeft] = useState(false);
  const [isDraggingCenter, setIsDraggingCenter] = useState(false);
  const [isDraggingRight, setIsDraggingRight] = useState(false);

  // Update panel widths when showCenterPanel changes
  useEffect(() => {
    if (showCenterPanel && centerWidth === 0) {
      // Load center panel width from storage or use default
      const stored = localStorage.getItem(STORAGE_KEY_3_PANEL);
      if (stored) {
        const { center, left } = JSON.parse(stored);
        setCenterWidth(center || 600);
        if (left) setLeftWidth(left);
      } else {
        setCenterWidth(600);
        setLeftWidth(300);
      }
    } else if (!showCenterPanel) {
      // Load 2-panel widths
      const stored = localStorage.getItem(STORAGE_KEY_2_PANEL);
      if (stored) {
        const { left } = JSON.parse(stored);
        if (left) setLeftWidth(left);
      } else {
        setLeftWidth(400);
      }
    }
  }, [showCenterPanel]);

  // Save to localStorage when widths change
  useEffect(() => {
    const storageKey = showCenterPanel ? STORAGE_KEY_3_PANEL : STORAGE_KEY_2_PANEL;
    const data = showCenterPanel
      ? { left: leftWidth, center: centerWidth, right: rightWidth }
      : { left: leftWidth, right: rightWidth };
    localStorage.setItem(storageKey, JSON.stringify(data));
  }, [leftWidth, centerWidth, rightWidth, showCenterPanel]);

  // Handle left divider drag
  const handleLeftMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsDraggingLeft(true);
  };

  // Handle center divider drag
  const handleCenterMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsDraggingCenter(true);
  };

  // Handle right divider drag
  const handleRightMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsDraggingRight(true);
  };

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!containerRef.current) return;

      const containerRect = containerRef.current.getBoundingClientRect();

      if (isDraggingLeft) {
        const newWidth = e.clientX - containerRect.left;
        const constrainedWidth = Math.max(
          MIN_PANEL_WIDTH,
          Math.min(MAX_PANEL_WIDTH, newWidth)
        );
        setLeftWidth(constrainedWidth);
      }

      if (isDraggingCenter && showCenterPanel) {
        // Calculate center width from left panel edge
        const newWidth = e.clientX - containerRect.left - leftWidth - 1; // -1 for divider
        const constrainedWidth = Math.max(
          MIN_PANEL_WIDTH,
          Math.min(MAX_PANEL_WIDTH, newWidth)
        );
        setCenterWidth(constrainedWidth);
      }

      if (isDraggingRight) {
        const newWidth = containerRect.right - e.clientX;
        const constrainedWidth = Math.max(
          MIN_PANEL_WIDTH,
          Math.min(MAX_PANEL_WIDTH, newWidth)
        );
        setRightWidth(constrainedWidth);
      }
    };

    const handleMouseUp = () => {
      setIsDraggingLeft(false);
      setIsDraggingCenter(false);
      setIsDraggingRight(false);
    };

    if (isDraggingLeft || isDraggingCenter || isDraggingRight) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleMouseUp);
      return () => {
        document.removeEventListener('mousemove', handleMouseMove);
        document.removeEventListener('mouseup', handleMouseUp);
      };
    }
  }, [isDraggingLeft, isDraggingCenter, isDraggingRight, leftWidth, showCenterPanel]);

  return (
    <div
      ref={containerRef}
      css={layoutStyles(leftWidth, centerWidth, rightWidth, showCenterPanel)}
      className={isDraggingLeft || isDraggingCenter || isDraggingRight ? 'dragging' : ''}
    >
      <div css={panelStyles} className="left-panel">
        {leftPanel}
      </div>

      {showCenterPanel && (
        <>
          <div
            css={dividerStyles}
            className="divider left-divider"
            onMouseDown={handleLeftMouseDown}
          />

          <div css={panelStyles} className="center-panel">
            {centerPanel}
          </div>

          <div
            css={dividerStyles}
            className="divider center-divider"
            onMouseDown={handleCenterMouseDown}
          />
        </>
      )}

      <div css={panelStyles} className="right-panel">
        {rightPanel}
      </div>
    </div>
  );
};

const layoutStyles = (leftWidth: number, centerWidth: number, rightWidth: number, showCenterPanel: boolean) => css`
  display: grid;
  grid-template-columns: ${showCenterPanel
    ? `${leftWidth}px 1px ${centerWidth}px 1px 1fr`
    : `${leftWidth}px 1fr`};
  height: calc(100vh - 64px); /* Subtract header height */
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

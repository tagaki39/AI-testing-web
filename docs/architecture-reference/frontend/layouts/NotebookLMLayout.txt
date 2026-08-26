import React from "react";
import { NotebookNav } from "../components/NotebookNav";

interface NotebookLMLayoutProps {
  leftPanel: React.ReactNode;
  centerPanel: React.ReactNode;
  /** New API: array of card elements rendered in the right panel */
  rightCards?: React.ReactNode[];
  /** Legacy API: single node rendered in the right panel (backward compat) */
  rightPanel?: React.ReactNode;
  navBottom?: boolean;
}

export function NotebookLMLayout({
  leftPanel,
  centerPanel,
  rightCards,
  rightPanel,
  navBottom = true,
}: NotebookLMLayoutProps) {
  // Support legacy rightPanel for backward compat
  const cards = rightCards ?? (rightPanel ? [rightPanel] : []);

  return (
    <div
      style={{
        display: "flex",
        height: "100vh",
        backgroundColor: "#f8f9fa",
        padding: 16,
        gap: 16,
        boxSizing: "border-box",
      }}
    >
      {/* Left Panel */}
      <div
        className="nb-card panel-scroll"
        style={{
          width: 280,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          padding: 16,
        }}
      >
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          {leftPanel}
        </div>
        {navBottom && <NotebookNav />}
      </div>

      {/* Center Panel */}
      <div
        className="nb-card"
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {centerPanel}
      </div>

      {/* Right Panel — transparent background, cards float in it */}
      <div
        style={{
          width: 340,
          display: "flex",
          flexDirection: "column",
          gap: 12,
          overflowY: "auto",
        }}
        className="panel-scroll"
      >
        {cards.map((card, index) => (
          <div key={index} className="nb-card" style={{ padding: 16 }}>
            {card}
          </div>
        ))}
      </div>
    </div>
  );
}

import React from "react";
import ReactDOM from "react-dom/client";
import { App as AntdApp, ConfigProvider } from "antd";

import { AppRoot } from "./app/App";
import "./index.css";
import "antd/dist/reset.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: "#1a1a2e",
          colorPrimaryActive: "#555",
          borderRadius: 8,
          fontFamily: "'Inter', 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', sans-serif",
          colorBgContainer: "#ffffff",
          colorBorderSecondary: "#f0f0f0",
        },
        components: {
          Button: {
            borderRadius: 8,
            colorPrimary: "#1a1a2e",
            primaryShadow: "0 2px 4px rgba(26,26,46,0.12)",
            defaultActiveBg: "#e0e0e0",
            defaultActiveBorderColor: "#bbb",
            defaultActiveColor: "#666",
          },
          Input: {
            borderRadius: 12,
            activeShadow: "0 0 0 2px rgba(26,26,46,0.08)",
            hoverBorderColor: "#d9d9d9",
          },
          Card: {
            borderRadius: 16,
            boxShadowTertiary: "0 2px 10px rgba(0,0,0,0.03)",
          },
          Table: {
            borderRadius: 12,
          },
          Select: {
            borderRadius: 12,
            optionSelectedBg: "#f0f4f8",
          },
          List: {},
          Collapse: {
            borderRadius: 12,
          },
          Tag: {
            borderRadiusSM: 12,
          },
        },
      }}
    >
      <AntdApp>
        <AppRoot />
      </AntdApp>
    </ConfigProvider>
  </React.StrictMode>,
);

import { useParams, useNavigate } from "react-router-dom";
import { AITestPlanningPanel } from "../components/AITestPlanningPanel";
import { useQuery } from "@tanstack/react-query";
import { getAISettings } from "../services/api";

export function PlanningPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const aiSettingsQuery = useQuery({ queryKey: ["ai-settings"], queryFn: getAISettings });

  if (!sessionId) {
    navigate("/planning");
    return null;
  }

  return (
    <AITestPlanningPanel
      aiSettings={aiSettingsQuery.data ?? null}
      sessionId={Number(sessionId)}
      onImportDraft={async () => {
        /* handled within panel via session projects */
      }}
    />
  );
}

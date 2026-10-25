import type { PendingApproval } from "../useAgent";

interface Props {
  approval: PendingApproval;
  onAnswer: (approved: boolean) => void;
}

// Shows the FULL arguments (recipient, subject, body...) so you know exactly what you approve.
export function ApprovalDialog({ approval, onAnswer }: Props) {
  return (
    <div className="overlay">
      <div className="dialog" role="dialog" aria-modal="true">
        <h2>Approval needed</h2>
        <p>
          The agent wants to run <code>{approval.name}</code> with:
        </p>
        <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
        <div className="actions">
          <button className="deny" onClick={() => onAnswer(false)}>
            Deny
          </button>
          <button className="approve" onClick={() => onAnswer(true)}>
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}

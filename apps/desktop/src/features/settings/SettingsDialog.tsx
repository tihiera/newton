// Settings: the profile, the Mac models switch, GitHub / Notion connections and the
// router key for other tools.

import { useProfile } from "../../app/data";
import { ErrorNote, Modal } from "../../components/ui";
import { ConnectionsSection } from "./Connections";
import { MacModelsSection, ProfileSection } from "./ProfileSection";
import { RouterAccess } from "./RouterAccess";
import "./settings.css";

export function SettingsDialog({ onClose }: { onClose: () => void }) {
  const profile = useProfile();
  return (
    <Modal onClose={onClose} label="Settings" wide>
      <div className="modal-body settings">
        <div className="mesh-header settings-top">
          <h2 className="h-display">Settings</h2>
          <div className="subtitle">
            Everything stays on this Mac. Tokens go to the Keychain and are never shown again.
          </div>
        </div>
        {profile.error && !profile.data ? <ErrorNote error={profile.error} /> : null}

        <h3 className="h-section settings-heading">Profile</h3>
        <ProfileSection profile={profile.data} refresh={profile.refresh} />
        <MacModelsSection profile={profile.data} refresh={profile.refresh} />

        <h3 className="h-section settings-heading">Connections</h3>
        <ConnectionsSection />

        <h3 className="h-section settings-heading">Router access for tools</h3>
        <RouterAccess />
      </div>
      <div className="modal-foot">
        <span className="spacer" />
        <button className="btn primary large" onClick={onClose}>
          Done
        </button>
      </div>
    </Modal>
  );
}

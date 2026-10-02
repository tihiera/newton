import { useEffect, useState } from "react";

/** Is the window visible? (Page Visibility API: a hidden or minimised window
 *  stops all polling, and polling resumes when it comes back.) */
export function useVisible(): boolean {
  const [visible, setVisible] = useState(
    typeof document === "undefined" ? true : document.visibilityState !== "hidden",
  );
  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

import type { ReactNode } from "react";

export const metadata = {
  title: "Session Brief",
  description: "Twice-daily market briefs for your own book.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

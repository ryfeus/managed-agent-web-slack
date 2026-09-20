import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Claude Managed Agent",
  description: "One durable Claude session across web and Slack"
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

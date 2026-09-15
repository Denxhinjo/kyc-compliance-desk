import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "KYC Review Desk — Demo",
  description: "Portfolio demo of a KYC onboarding and compliance review flow.",
};

/**
 * The root layout wraps every page in the app. The synthetic-data banner lives
 * here so it cannot be forgotten on a page added later.
 */
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <div className="demo-banner">Demo — synthetic data only</div>
        {children}
      </body>
    </html>
  );
}

import type { Metadata } from "next";
import "./globals.css";
import { SiteChrome } from "@/components/SiteChrome";

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
        {/* THE BANNER STAYS A BANNER.
            The reference reduces this to a small "Demo" pill in the corner.
            That is the right weight for a product demo and the wrong weight
            here: every applicant, score and decision on this site is invented,
            and a visitor who misses that could read the stats page as a claim
            about real onboarding. The pill is in the bar as well, for anyone
            who scrolls past this, but the full-width strip is not negotiable. */}
        <div className="demo-banner">Demo — synthetic data only</div>
        <SiteChrome />
        {children}
      </body>
    </html>
  );
}

import type { MetadataRoute } from "next";

/**
 * What crawlers may index.
 *
 * The public pages are fine and, for a portfolio demo, the point: the home
 * page, the review desk, the stats. They describe the project rather than
 * anybody in it.
 *
 * `/status/<uuid>` is different. It is addressed only by an application's
 * UUID, needs no login, and shows one applicant's name, the stage they have
 * reached and the outcome. Every applicant here is invented, so nothing real
 * is at stake — but a system that would put a customer's onboarding status
 * into a search index if the data were real has a design flaw, and the demo
 * should not be modelling that. It is excluded here AND carries `noindex` on
 * the page itself; belt and braces, because robots.txt is a request and the
 * meta tag is the one honoured by a crawler that already holds the URL.
 *
 * The vendor simulator goes too. It is scaffolding standing in for a
 * third party, and a search result pointing at a fake identity provider
 * would be confusing at best.
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: "/",
      disallow: ["/status/", "/mock-vendor/", "/api/", "/verify/"],
    },
  };
}

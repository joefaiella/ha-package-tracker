# Changelog

## 0.3.0

- Bundled dashboard card, `custom:package-tracker-card`, loaded automatically. It shows count tiles that filter the list, a tick box for each package, and updates live.
- Click-to-track links that open the carrier's tracking page (USPS, UPS, FedEx, DHL, OnTrac, GOFO), Amazon's tracking or order page, or the email in Gmail.
- After upgrading, recent mail is re-read once to link existing packages to their emails.

## 0.2.0

- "Delivery address" option to show only packages going to your addresses, for store accounts shared with other households.
- Options to show packages whose emails don't name an address, and to always show USPS Informed Delivery packages.
- After upgrading, recent mail is re-read once to learn the delivery addresses of existing packages.

## 0.1.0

- Initial release.
- Reads shipping emails from Gmail over read-only IMAP. Covers Amazon, USPS (including Informed Delivery), UPS, FedEx and online stores.
- Keeps its own package registry, so packages stay listed after their emails are deleted.
- Sensors for packages arriving today, out for delivery, on the way, delivered today and ordered, plus today's mail count.
- A Packages to-do list, where ticking a package marks it received.
- Voice assistant tool for questions like "what's arriving today?".

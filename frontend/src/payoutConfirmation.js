const money = (cents) => new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD',
}).format(cents / 100)

export function payoutPreviewValid(totalCents, expectedCount, recipients) {
  return recipients.length === expectedCount
    && recipients.reduce((total, recipient) => total + recipient.amount_cents, 0) === totalCents
}

export function payoutConfirmationMessage(totalCents, recipients) {
  const lines = recipients.map((recipient) =>
    `${recipient.name} (${recipient.project}): ${money(recipient.amount_cents)}`)
  return `Submit ${money(totalCents)} USD in PayPal Sandbox payouts?\n\nRecipients:\n${lines.join('\n')}`
}

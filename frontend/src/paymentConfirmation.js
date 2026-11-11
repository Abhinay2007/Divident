const money = (cents) => new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD',
}).format(cents / 100)

export function captureConfirmationMessage(amountCents) {
  return `Capture the approved PayPal order for ${money(amountCents)} USD?`
}

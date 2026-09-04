import { render, screen } from '@testing-library/react'
import App from './App'

test('renders safety-first demo terminal', () => {
  render(<App />)
  expect(screen.getAllByText('EPSILON').length).toBeGreaterThan(0)
  expect(screen.getAllByText('PAPER TRADING').length).toBeGreaterThan(0)
  expect(screen.getAllByText('DEMO / SIMULATED').length).toBeGreaterThan(0)
  expect(screen.getByText('Guardrails first.')).toBeTruthy()
})

test('homepage shows pipeline, ticker, risk posture, and honest demo labels', () => {
  render(<App />)
  // The decision pipeline is visible.
  expect(screen.getAllByText('MARKET DATA').length).toBeGreaterThan(0)
  expect(screen.getByText('TECHNICAL SIGNALS')).toBeTruthy()
  expect(screen.getByText('AI DECISION')).toBeTruthy()
  expect(screen.getByText('PYTHON SIZING')).toBeTruthy()
  expect(screen.getByText('RISK CHECK')).toBeTruthy()
  expect(screen.getByText('FINAL GATE')).toBeTruthy()
  expect(screen.getByText('PAPER EXECUTION')).toBeTruthy()
  // The ticker is labeled as demo.
  expect(screen.getByText('DEMO MARKET DATA')).toBeTruthy()
  expect(screen.queryByText(/live orders/i)).toBeNull()
  expect(screen.queryByText(/real-time execution/i)).toBeNull()
  // Risk posture is visible.
  expect(screen.getAllByText('SAFE').length).toBeGreaterThan(0)
  expect(screen.getByText('Risk posture')).toBeTruthy()
  // Demo decision values are shown.
  expect(screen.getByText('4 shares')).toBeTruthy()
})


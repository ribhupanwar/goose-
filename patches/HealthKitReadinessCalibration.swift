// Goose readiness calibration, inspired by published robust-statistics practice.
// Independent Swift implementation; no OpenStrap source code is copied.
import Foundation

enum HealthKitReadinessCalibration {
  struct Result {
    let score: Double
    let status: String
    let nights: Int
  }

  private static let key = "goose.readiness.composite-z-history.v1"
  private static let minimumNights = 14
  private static let sigmaFloor = 0.3
  private static let designSigma = 0.65
  private static let centreCap = 0.5

  static func score(_ z: Double, at date: Date) -> Result {
    let defaults = UserDefaults.standard
    let calendar = Calendar(identifier: .gregorian)
    let today = calendar.startOfDay(for: date).timeIntervalSince1970
    let cutoff = calendar.date(byAdding: .day, value: -30, to: date)!
      .timeIntervalSince1970

    var history = (defaults.array(forKey: key) as? [[String: Double]] ?? [])
      .filter { ($0["day"] ?? 0) >= cutoff && ($0["day"] ?? 0) < today }
      .sorted { ($0["day"] ?? 0) < ($1["day"] ?? 0) }

    let nights = history.count
    var result: Result
    if nights < minimumNights {
      let x = z.clamped(to: -20...20)
      let value = 100 / (1 + exp(-x))
      result = Result(score: value, status: "Calibrating", nights: nights)
    } else {
      let prior = history.compactMap { $0["z"] }
      let centerRaw = median(prior) ?? 0
      let deviations = prior.map { abs($0 - centerRaw) }
      let sigma = max(sigmaFloor, (median(deviations) ?? 0) * 1.4826)
      let center = centerRaw.clamped(to: -centreCap...centreCap)
      let x = (designSigma * (z - center) / sigma).clamped(to: -20...20)
      let value = 100 / (1 + exp(-x))
      result = Result(score: value, status: "Calibrated", nights: nights)
    }

    // Persist at most one composite per local calendar day. Re-rendering is
    // idempotent: today's entry is replaced, never counted as a prior night.
    history.removeAll { ($0["day"] ?? 0) == today }
    history.append(["day": today, "z": z])
    let keepAfter = calendar.date(byAdding: .day, value: -45, to: date)!
      .timeIntervalSince1970
    defaults.set(history.filter { ($0["day"] ?? 0) >= keepAfter }, forKey: key)
    return result
  }

  private static func median(_ input: [Double]) -> Double? {
    guard !input.isEmpty else { return nil }
    let values = input.sorted()
    let mid = values.count / 2
    return values.count.isMultiple(of: 2)
      ? (values[mid - 1] + values[mid]) / 2
      : values[mid]
  }
}

private extension Double {
  func clamped(to range: ClosedRange<Double>) -> Double {
    min(max(self, range.lowerBound), range.upperBound)
  }
}

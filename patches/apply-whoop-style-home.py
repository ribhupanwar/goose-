import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
home_matches = list(root.rglob("HomeDashboardView.swift"))
score_matches = list(root.rglob("HomeScoreViews.swift"))
if len(home_matches) != 1 or len(score_matches) != 1:
    raise SystemExit(f"Expected one HomeDashboardView.swift and HomeScoreViews.swift; found {len(home_matches)} and {len(score_matches)}")
home = home_matches[0]
score = score_matches[0]

home_text = home.read_text()
score_text = score.read_text()

old = """        HomeDailyScoreCard(
          scores: scoreSnapshots,
          actionSummary: dailyActionSummary,
          coachTip: CoachTipFactory.homeTip(healthStore: healthStore, appModel: model),
          openScore: openHealth,
          openCoach: openCoach
        )
"""
new = """        WhoopStyleHomeOverview(
          recovery: homeSnapshot(for: .recovery),
          sleep: homeSnapshot(for: .sleep),
          strain: homeSnapshot(for: .strain),
          stress: landingSnapshot(for: .stress),
          openMetric: openHealth
        )

        CoachTipCard(
          tip: CoachTipFactory.homeTip(healthStore: healthStore, appModel: model)
        ) {
          openCoach(CoachTipFactory.homeTip(healthStore: healthStore, appModel: model).prompt)
        }
"""
if old not in home_text:
    raise SystemExit("HomeDashboardView score-card anchor not found; refusing to modify.")
if "struct WhoopStyleHomeOverview: View" not in score_text:
    score_text += '''

struct WhoopStyleHomeOverview: View {
  let recovery: HealthMetricSnapshot
  let sleep: HealthMetricSnapshot
  let strain: HealthMetricSnapshot
  let stress: HealthMetricSnapshot
  let openMetric: (HealthRoute) -> Void

  var body: some View {
    VStack(spacing: 18) {
      VStack(spacing: 10) {
        HStack {
          Text("GOOSE")
            .font(.system(size: 17, weight: .black, design: .rounded))
            .tracking(2.0)
          Spacer()
          Label("YOUR DAY", systemImage: "sun.max.fill")
            .font(.caption2.weight(.bold))
            .tracking(1.1)
            .foregroundStyle(.secondary)
        }
        HStack {
          VStack(alignment: .leading, spacing: 4) {
            Text("RECOVERY")
              .font(.caption.weight(.bold))
              .tracking(1.4)
              .foregroundStyle(.secondary)
            Text(recovery.status.isEmpty ? "Daily readiness" : recovery.status)
              .font(.subheadline.weight(.semibold))
              .foregroundStyle(recovery.tint)
          }
          Spacer()
          Button { openMetric(.recovery) } label: {
            Image(systemName: "arrow.up.right")
              .font(.system(size: 13, weight: .bold))
              .foregroundStyle(.secondary)
              .frame(width: 32, height: 32)
              .background(Color.primary.opacity(0.07), in: Circle())
          }
          .buttonStyle(.plain)
          .accessibilityLabel("Open Recovery details")
        }
        ZStack {
          Circle().stroke(recovery.tint.opacity(0.16), lineWidth: 13)
          Circle()
            .trim(from: 0, to: recoveryProgress)
            .stroke(recovery.tint, style: StrokeStyle(lineWidth: 13, lineCap: .round))
            .rotationEffect(.degrees(-90))
          VStack(spacing: 2) {
            Text(recoveryValue)
              .font(.system(size: 42, weight: .bold, design: .rounded))
              .monospacedDigit()
              .contentTransition(.numericText())
            Text(recovery.unit.isEmpty ? "SCORE" : recovery.unit.uppercased())
              .font(.caption2.weight(.bold))
              .tracking(1.2)
              .foregroundStyle(.secondary)
          }
        }
        .frame(width: 174, height: 174)
        .padding(.vertical, 4)
        Text(recovery.provenance.isEmpty ? "Based on available Goose data" : recovery.provenance)
          .font(.caption2)
          .foregroundStyle(.secondary)
          .multilineTextAlignment(.center)
      }
      .padding(18)
      .frame(maxWidth: .infinity)
      .background(
        LinearGradient(
          colors: [Color(red: 0.07, green: 0.13, blue: 0.17), Color(red: 0.035, green: 0.055, blue: 0.075)],
          startPoint: .topLeading,
          endPoint: .bottomTrailing
        ),
        in: RoundedRectangle(cornerRadius: 24, style: .continuous)
      )
      .overlay {
        RoundedRectangle(cornerRadius: 24, style: .continuous)
          .strokeBorder(Color.white.opacity(0.07), lineWidth: 1)
      }

      HStack(spacing: 10) {
        metricTile(sleep, route: .sleep, tint: Color(red: 0.12, green: 0.66, blue: 0.98))
        metricTile(strain, route: .strain, tint: Color(red: 1.0, green: 0.65, blue: 0.12))
        metricTile(stress, route: .stress, tint: Color(red: 0.66, green: 0.35, blue: 0.98))
      }
    }
  }

  private func metricTile(_ snapshot: HealthMetricSnapshot, route: HealthRoute, tint: Color) -> some View {
    Button { openMetric(route) } label: {
      VStack(alignment: .leading, spacing: 8) {
        HStack(spacing: 5) {
          Circle().fill(tint).frame(width: 7, height: 7)
          Text(snapshot.title.uppercased())
            .font(.system(size: 9, weight: .bold))
            .tracking(0.7)
            .lineLimit(1)
            .minimumScaleFactor(0.7)
        }
        Text(snapshot.displayValue)
          .font(.system(size: 20, weight: .bold, design: .rounded))
          .monospacedDigit()
          .lineLimit(1)
          .minimumScaleFactor(0.65)
        Text(snapshot.status.isEmpty ? snapshot.freshness : snapshot.status)
          .font(.system(size: 10, weight: .medium))
          .foregroundStyle(.secondary)
          .lineLimit(1)
      }
      .frame(maxWidth: .infinity, alignment: .leading)
      .padding(12)
      .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
      .overlay {
        RoundedRectangle(cornerRadius: 16, style: .continuous)
          .strokeBorder(tint.opacity(0.18), lineWidth: 1)
      }
      .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
    .buttonStyle(.plain)
    .accessibilityLabel("Open \(snapshot.title): \(snapshot.displayValue)")
  }

  private var recoveryValue: String {
    recovery.displayValue.replacingOccurrences(of: "%", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
  }

  private var recoveryProgress: Double {
    guard let value = Double(recoveryValue) else { return 0 }
    return min(max(value / 100, 0), 1)
  }
}
'''
home.write_text(home_text.replace(old, new))
score.write_text(score_text)
print("Applied WHOOP-inspired Home overview: recovery hero + Sleep/Strain/Stress quick metrics.")

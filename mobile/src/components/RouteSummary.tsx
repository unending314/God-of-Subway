import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { colors } from '../theme';
import type { AutoRouteResponse, RouteResponse } from '../types/api';
import { formatClock, formatDuration } from '../utils/time';

type Props = { auto: AutoRouteResponse; detail?: RouteResponse | null };

export function RouteSummary({ auto, detail }: Props) {
  const total = detail?.total_seconds ?? auto.estimated_total_seconds ?? auto.route_seconds;
  const arrival = detail?.arrival_time ?? auto.estimated_arrival_time;
  const confidence = auto.estimated_confidence || '-';

  return (
    <View style={styles.card}>
      <Text style={styles.route}>{auto.from} → {auto.to}</Text>
      <View style={styles.metrics}>
        <Metric label="예상 소요" value={formatDuration(total)} />
        <Metric label="도착 예정" value={formatClock(arrival)} />
        <Metric label="환승" value={`${auto.transfer_count}회`} />
      </View>
      <Text style={styles.meta}>신뢰도 {confidence}{auto.interchanges?.length ? ` · ${auto.interchanges.join(' → ')}` : ' · 무환승'}</Text>
    </View>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.metric}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={styles.metricValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { backgroundColor: colors.accentDim, borderRadius: 18, padding: 18, borderWidth: 1, borderColor: '#245D4A' },
  route: { color: colors.text, fontSize: 19, fontWeight: '800', marginBottom: 14 },
  metrics: { flexDirection: 'row', gap: 8 },
  metric: { flex: 1 },
  metricLabel: { color: colors.muted, fontSize: 11 },
  metricValue: { color: colors.text, fontSize: 17, fontWeight: '800', marginTop: 2 },
  meta: { color: colors.muted, fontSize: 13, marginTop: 14 },
});

import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { colors } from '../theme';
import type { DetailedSegment } from '../types/api';
import { formatClock, formatDuration, formatTransfer } from '../utils/time';

type Props = { segment: DetailedSegment; index: number; isLast: boolean };

export function SegmentCard({ segment, index, isLast }: Props) {
  const train = segment.train_no_visible === false
    ? '운행편'
    : (segment.display_train_no || segment.train_no || '열차');
  const delay = segment.delay_seconds ?? segment.delay;
  const delayText = typeof delay === 'number'
    ? (Math.abs(delay) < 60 ? '정시권' : `${delay > 0 ? '+' : ''}${Math.round(delay / 60)}분`)
    : '미산정';
  const material = segment.material_text || segment.current_station || (segment.location_kind === 'live' ? '실시간 위치' : '시간표 예상');
  const transferSec = segment.transfer_info?.seconds ?? segment.transfer_seconds;

  return (
    <View>
      <View style={styles.card}>
        <View style={styles.topRow}>
          <View style={styles.lineBadge}><Text style={styles.lineText}>{segment.line}</Text></View>
          <Text style={styles.order}>{index + 1}구간</Text>
        </View>
        <Text style={styles.stations}>{segment.from} <Text style={styles.time}>({formatClock(segment.board_dt)})</Text> → {segment.to} <Text style={styles.time}>({formatClock(segment.alight_dt)})</Text></Text>
        <Text style={styles.ride}>{formatDuration(segment.ride_seconds)} 이동 · 신뢰도 {segment.confidence || '-'}</Text>

        <View style={styles.infoBox}>
          <InfoRow label="열차" value={`${train}${segment.service === 'express' ? ' · 급행' : ''}`} />
          <InfoRow label="현재 소재" value={material} />
          <InfoRow label="실시간 지연" value={delayText} />
        </View>
      </View>
      {!isLast && transferSec != null && transferSec > 0 ? (
        <View style={styles.transfer}>
          <Text style={styles.transferTitle}>↓ 환승시간 {formatTransfer(transferSec)}</Text>
          {segment.transfer_info?.alight_position ? <Text style={styles.transferMeta}>빠른 환승 · {segment.transfer_info.alight_position}</Text> : null}
        </View>
      ) : null}
    </View>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.infoRow}>
      <Text style={styles.infoLabel}>{label}</Text>
      <Text style={styles.infoValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.border, borderRadius: 18, padding: 16 },
  topRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 },
  lineBadge: { backgroundColor: '#1B2E3B', paddingHorizontal: 10, paddingVertical: 6, borderRadius: 10 },
  lineText: { color: colors.text, fontWeight: '800', fontSize: 13 },
  order: { color: colors.muted, fontSize: 12 },
  stations: { color: colors.text, fontSize: 18, fontWeight: '800', lineHeight: 27 },
  time: { color: colors.accent, fontSize: 15 },
  ride: { color: colors.muted, marginTop: 6, marginBottom: 14 },
  infoBox: { backgroundColor: colors.surfaceElevated, borderRadius: 12, padding: 12, gap: 8 },
  infoRow: { flexDirection: 'row', alignItems: 'flex-start' },
  infoLabel: { width: 84, color: colors.muted, fontSize: 13 },
  infoValue: { flex: 1, color: colors.text, fontWeight: '600', fontSize: 13 },
  transfer: { paddingHorizontal: 12, paddingVertical: 12, marginVertical: 5, borderLeftWidth: 2, borderLeftColor: colors.accent },
  transferTitle: { color: colors.text, fontWeight: '800' },
  transferMeta: { color: colors.muted, marginTop: 3, fontSize: 12 },
});

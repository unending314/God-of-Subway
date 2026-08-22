import React, { useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  SafeAreaView,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { api, API_BASE } from './src/api/client';
import { RouteSummary } from './src/components/RouteSummary';
import { SegmentCard } from './src/components/SegmentCard';
import { StationPicker } from './src/components/StationPicker';
import { colors } from './src/theme';
import type { AutoRouteResponse, RouteResponse } from './src/types/api';
import { hhmm, formatClock, formatDuration } from './src/utils/time';

type StationItem = { station: string; lines: string[] };

export default function App() {
  const [stations, setStations] = useState<StationItem[]>([]);
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [startTime, setStartTime] = useState(hhmm());
  const [version, setVersion] = useState('연결 확인 중');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [autoResult, setAutoResult] = useState<AutoRouteResponse | null>(null);
  const [detailResult, setDetailResult] = useState<RouteResponse | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([api.health(), api.stations()])
      .then(([health, stationResponse]) => {
        if (!alive) return;
        setVersion(health.version || 'API 연결됨');
        const map = new Map<string, Set<string>>();
        Object.entries(stationResponse.stations || {}).forEach(([line, names]) => {
          (names || []).forEach((station) => {
            const set = map.get(station) || new Set<string>();
            set.add(line);
            map.set(station, set);
          });
        });
        setStations(
          Array.from(map.entries())
            .map(([station, lines]) => ({ station, lines: Array.from(lines).sort() }))
            .sort((a, b) => a.station.localeCompare(b.station, 'ko')),
        );
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setVersion('API 연결 실패');
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => { alive = false; };
  }, []);

  const canSearch = useMemo(() => Boolean(from && to && from !== to && /^\d{2}:\d{2}$/.test(startTime)), [from, to, startTime]);

  async function findRoute() {
    if (!canSearch) return;
    setLoading(true);
    setError('');
    setAutoResult(null);
    setDetailResult(null);
    try {
      const auto = await api.autoRoute(from, to, startTime, 'AUTO');
      if (!auto.ok) throw new Error(auto.error || '자동 경로 탐색 실패');
      setAutoResult(auto);
      const detail = await api.route(auto.segments, startTime, 'AUTO');
      if (!detail.ok) throw new Error(detail.error || '상세 ETA 계산 실패');
      setDetailResult(detail);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <SafeAreaView style={styles.safe}>
      <StatusBar barStyle="light-content" />
      <ScrollView contentContainerStyle={styles.container} keyboardShouldPersistTaps="handled">
        <View style={styles.header}>
          <View>
            <Text style={styles.brand}>지금타</Text>
            <Text style={styles.subtitle}>라이브 지하철 ETA</Text>
          </View>
          <View style={styles.apiBadge}>
            <Text style={styles.apiDot}>●</Text>
            <Text style={styles.apiText}>{version}</Text>
          </View>
        </View>

        <View style={styles.searchCard}>
          <View style={styles.stationRow}>
            <StationPicker label="출발역" value={from} items={stations} onSelect={setFrom} />
            <Pressable
              style={styles.swap}
              onPress={() => { const next = from; setFrom(to); setTo(next); }}
              accessibilityLabel="출발역과 도착역 바꾸기"
            >
              <Text style={styles.swapText}>⇄</Text>
            </Pressable>
            <StationPicker label="도착역" value={to} items={stations} onSelect={setTo} />
          </View>

          <View style={styles.timeRow}>
            <View style={styles.timeFieldWrap}>
              <Text style={styles.smallLabel}>출발 시각</Text>
              <TextInput
                value={startTime}
                onChangeText={setStartTime}
                style={styles.timeInput}
                keyboardType="numbers-and-punctuation"
                maxLength={5}
                placeholder="HH:MM"
                placeholderTextColor={colors.muted}
              />
            </View>
            <Pressable style={styles.nowButton} onPress={() => setStartTime(hhmm())}>
              <Text style={styles.nowButtonText}>지금</Text>
            </Pressable>
          </View>

          <Pressable style={[styles.searchButton, !canSearch && styles.searchButtonDisabled]} disabled={!canSearch || loading} onPress={findRoute}>
            {loading ? <ActivityIndicator color="#071018" /> : <Text style={styles.searchButtonText}>경로 조회</Text>}
          </Pressable>
        </View>

        {error ? <View style={styles.errorBox}><Text style={styles.errorText}>{error}</Text></View> : null}

        {autoResult ? (
          <View style={styles.results}>
            <RouteSummary auto={autoResult} detail={detailResult} />

            {detailResult?.segments?.map((segment, index, all) => (
              <SegmentCard key={`${segment.line}-${index}-${segment.board_dt}`} segment={segment} index={index} isLast={index === all.length - 1} />
            ))}

            {autoResult.alternatives?.length ? (
              <View style={styles.altBox}>
                <Text style={styles.sectionTitle}>다른 경로</Text>
                {autoResult.alternatives.slice(0, 3).map((alt, index) => (
                  <View key={`${alt.arrival_time}-${index}`} style={styles.altRow}>
                    <View style={{ flex: 1 }}>
                      <Text style={styles.altTitle}>{index + 2}안 · 환승 {alt.transfer_count}회</Text>
                      <Text style={styles.altMeta}>{alt.interchanges?.length ? alt.interchanges.join(' → ') : '무환승'}</Text>
                    </View>
                    <View style={styles.altTime}>
                      <Text style={styles.altArrival}>{formatClock(alt.arrival_time)}</Text>
                      <Text style={styles.altDuration}>{formatDuration(alt.total_seconds)}</Text>
                    </View>
                  </View>
                ))}
              </View>
            ) : null}
          </View>
        ) : (
          <View style={styles.emptyState}>
            <Text style={styles.emptyTitle}>출발역과 도착역을 선택하세요</Text>
            <Text style={styles.emptyText}>현재 운행상황과 공식 시간표를 함께 반영해 도착 예정시각을 계산합니다.</Text>
          </View>
        )}

        <Text style={styles.endpoint}>API · {API_BASE}</Text>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.background },
  container: { padding: 18, paddingBottom: 42, gap: 16 },
  header: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingTop: 8 },
  brand: { color: colors.text, fontSize: 30, fontWeight: '900', letterSpacing: -1 },
  subtitle: { color: colors.muted, fontSize: 13, marginTop: 2 },
  apiBadge: { flexDirection: 'row', alignItems: 'center', gap: 5, backgroundColor: colors.surface, borderRadius: 999, paddingHorizontal: 10, paddingVertical: 7, borderWidth: 1, borderColor: colors.border, maxWidth: 150 },
  apiDot: { color: colors.accent, fontSize: 9 },
  apiText: { color: colors.muted, fontSize: 11, flexShrink: 1 },
  searchCard: { backgroundColor: colors.surface, borderRadius: 22, padding: 14, borderWidth: 1, borderColor: colors.border, gap: 12 },
  stationRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  swap: { width: 38, height: 38, borderRadius: 19, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.surfaceElevated, borderWidth: 1, borderColor: colors.border },
  swapText: { color: colors.accent, fontSize: 20, fontWeight: '800' },
  timeRow: { flexDirection: 'row', alignItems: 'flex-end', gap: 10 },
  timeFieldWrap: { flex: 1 },
  smallLabel: { color: colors.muted, fontSize: 12, marginBottom: 5 },
  timeInput: { height: 48, borderRadius: 13, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.surfaceElevated, color: colors.text, fontSize: 18, fontWeight: '700', paddingHorizontal: 14 },
  nowButton: { height: 48, minWidth: 72, borderRadius: 13, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.surfaceElevated, borderWidth: 1, borderColor: colors.border },
  nowButtonText: { color: colors.accent, fontWeight: '800' },
  searchButton: { height: 54, borderRadius: 15, backgroundColor: colors.accent, alignItems: 'center', justifyContent: 'center' },
  searchButtonDisabled: { opacity: 0.35 },
  searchButtonText: { color: '#071018', fontSize: 17, fontWeight: '900' },
  results: { gap: 12 },
  errorBox: { padding: 14, borderRadius: 14, backgroundColor: '#35191B', borderWidth: 1, borderColor: '#6F3035' },
  errorText: { color: '#FFC9CC', fontWeight: '600' },
  emptyState: { paddingVertical: 58, paddingHorizontal: 24, alignItems: 'center' },
  emptyTitle: { color: colors.text, fontSize: 18, fontWeight: '800' },
  emptyText: { color: colors.muted, marginTop: 8, textAlign: 'center', lineHeight: 20 },
  altBox: { backgroundColor: colors.surface, borderRadius: 18, padding: 16, borderWidth: 1, borderColor: colors.border },
  sectionTitle: { color: colors.text, fontSize: 16, fontWeight: '800', marginBottom: 8 },
  altRow: { flexDirection: 'row', alignItems: 'center', paddingVertical: 12, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.border },
  altTitle: { color: colors.text, fontWeight: '700' },
  altMeta: { color: colors.muted, fontSize: 12, marginTop: 3 },
  altTime: { alignItems: 'flex-end', marginLeft: 10 },
  altArrival: { color: colors.accent, fontSize: 16, fontWeight: '800' },
  altDuration: { color: colors.muted, fontSize: 11, marginTop: 2 },
  endpoint: { color: '#536774', fontSize: 10, textAlign: 'center', marginTop: 8 },
});

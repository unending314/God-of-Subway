import React, { useMemo, useState } from 'react';
import {
  FlatList,
  Modal,
  Pressable,
  SafeAreaView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { colors } from '../theme';

type StationItem = { station: string; lines: string[] };

type Props = {
  label: string;
  value: string;
  items: StationItem[];
  onSelect: (station: string) => void;
};

export function StationPicker({ label, value, items, onSelect }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items.slice(0, 80);
    return items.filter((item) => item.station.toLowerCase().includes(q)).slice(0, 80);
  }, [items, query]);

  return (
    <>
      <Pressable style={styles.field} onPress={() => setOpen(true)}>
        <Text style={styles.label}>{label}</Text>
        <Text style={[styles.value, !value && styles.placeholder]}>{value || `${label} 선택`}</Text>
      </Pressable>

      <Modal visible={open} animationType="slide" onRequestClose={() => setOpen(false)}>
        <SafeAreaView style={styles.modalRoot}>
          <View style={styles.modalHeader}>
            <Text style={styles.modalTitle}>{label} 검색</Text>
            <Pressable onPress={() => setOpen(false)} hitSlop={12}>
              <Text style={styles.close}>닫기</Text>
            </Pressable>
          </View>
          <TextInput
            autoFocus
            value={query}
            onChangeText={setQuery}
            placeholder="역 이름 입력"
            placeholderTextColor={colors.muted}
            style={styles.search}
            returnKeyType="search"
          />
          <FlatList
            data={filtered}
            keyExtractor={(item) => item.station}
            keyboardShouldPersistTaps="handled"
            renderItem={({ item }) => (
              <Pressable
                style={styles.row}
                onPress={() => {
                  onSelect(item.station);
                  setQuery('');
                  setOpen(false);
                }}
              >
                <Text style={styles.station}>{item.station}</Text>
                <Text style={styles.lines}>{item.lines.join(' · ')}</Text>
              </Pressable>
            )}
            ListEmptyComponent={<Text style={styles.empty}>검색 결과가 없습니다.</Text>}
          />
        </SafeAreaView>
      </Modal>
    </>
  );
}

const styles = StyleSheet.create({
  field: {
    flex: 1,
    minHeight: 72,
    justifyContent: 'center',
    paddingHorizontal: 16,
    paddingVertical: 10,
    backgroundColor: colors.surfaceElevated,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 16,
  },
  label: { color: colors.muted, fontSize: 12, marginBottom: 4 },
  value: { color: colors.text, fontSize: 20, fontWeight: '700' },
  placeholder: { color: colors.muted, fontWeight: '500' },
  modalRoot: { flex: 1, backgroundColor: colors.background, paddingHorizontal: 16 },
  modalHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingVertical: 16 },
  modalTitle: { color: colors.text, fontSize: 22, fontWeight: '800' },
  close: { color: colors.accent, fontWeight: '700', fontSize: 16 },
  search: {
    height: 52,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    borderRadius: 14,
    color: colors.text,
    fontSize: 17,
    paddingHorizontal: 14,
    marginBottom: 10,
  },
  row: { paddingVertical: 15, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.border },
  station: { color: colors.text, fontWeight: '700', fontSize: 17 },
  lines: { color: colors.muted, marginTop: 4, fontSize: 13 },
  empty: { color: colors.muted, textAlign: 'center', marginTop: 40 },
});

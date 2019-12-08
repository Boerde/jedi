from jedi.inference.cache import inference_state_method_cache
from jedi.inference.base_value import ValueSet, NO_VALUES, Value, \
    iterator_to_value_set, LazyValueWrapper, ValueWrapper
from jedi.inference.compiled import builtin_from_name
from jedi.inference.value.klass import ClassFilter
from jedi.inference.value.iterable import SequenceLiteralValue
from jedi.inference.value.klass import ClassMixin
from jedi.inference.utils import to_list
from jedi.inference.names import AbstractNameDefinition, ValueName
from jedi.inference.context import ClassContext
from jedi.inference.helpers import is_string


def iter_over_arguments(maybe_tuple_value, defining_context):
    def iterate():
        if isinstance(maybe_tuple_value, SequenceLiteralValue):
            for lazy_value in maybe_tuple_value.py__iter__(contextualized_node=None):
                yield lazy_value.infer()
        else:
            yield ValueSet([maybe_tuple_value])

    def resolve_forward_references(value_set):
        for value in value_set:
            if is_string(value):
                from jedi.inference.gradual.annotation import _get_forward_reference_node
                node = _get_forward_reference_node(defining_context, value.get_safe_value())
                if node is not None:
                    for c in defining_context.infer_node(node):
                        yield c
            else:
                yield value

    for value_set in iterate():
        yield ValueSet(resolve_forward_references(value_set))


class BaseTypingValue(Value):
    def __init__(self, inference_state, parent_context, tree_name):
        super(BaseTypingValue, self).__init__(inference_state, parent_context)
        self._tree_name = tree_name

    @property
    def tree_node(self):
        return self._tree_name

    def get_filters(self, *args, **kwargs):
        # TODO this is obviously wrong. Is it though?
        class EmptyFilter(ClassFilter):
            def __init__(self):
                pass

            def get(self, name, **kwargs):
                return []

            def values(self, **kwargs):
                return []

        yield EmptyFilter()

    def py__class__(self):
        # TODO this is obviously not correct, but at least gives us a class if
        # we have none. Some of these objects don't really have a base class in
        # typeshed.
        return builtin_from_name(self.inference_state, u'object')

    @property
    def name(self):
        return ValueName(self, self._tree_name)

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self._tree_name.value)


class BoundTypeVarName(AbstractNameDefinition):
    """
    This type var was bound to a certain type, e.g. int.
    """
    def __init__(self, type_var, value_set):
        self._type_var = type_var
        self.parent_context = type_var.parent_context
        self._value_set = value_set

    def infer(self):
        def iter_():
            for value in self._value_set:
                # Replace any with the constraints if they are there.
                from jedi.inference.gradual.typing import Any
                if isinstance(value, Any):
                    for constraint in self._type_var.constraints:
                        yield constraint
                else:
                    yield value
        return ValueSet(iter_())

    def py__name__(self):
        return self._type_var.py__name__()

    def __repr__(self):
        return '<%s %s -> %s>' % (self.__class__.__name__, self.py__name__(), self._value_set)


class TypeVarFilter(object):
    """
    A filter for all given variables in a class.

        A = TypeVar('A')
        B = TypeVar('B')
        class Foo(Mapping[A, B]):
            ...

    In this example we would have two type vars given: A and B
    """
    def __init__(self, generics, type_vars):
        self._generics = generics
        self._type_vars = type_vars

    def get(self, name):
        for i, type_var in enumerate(self._type_vars):
            if type_var.py__name__() == name:
                try:
                    return [BoundTypeVarName(type_var, self._generics[i])]
                except IndexError:
                    return [type_var.name]
        return []

    def values(self):
        # The values are not relevant. If it's not searched exactly, the type
        # vars are just global and should be looked up as that.
        return []


class AnnotatedClassContext(ClassContext):
    def get_filters(self, *args, **kwargs):
        filters = super(AnnotatedClassContext, self).get_filters(
            *args, **kwargs
        )
        for f in filters:
            yield f

        # The type vars can only be looked up if it's a global search and
        # not a direct lookup on the class.
        yield self._value.get_type_var_filter()


class DefineGenericBase(LazyValueWrapper):
    def _get_fixed_generics_cls(self):
        return GenericClass

    def get_generics(self):
        raise NotImplementedError

    def define_generics(self, type_var_dict):
        from jedi.inference.gradual.type_var import TypeVar
        changed = False
        new_generics = []
        for generic_set in self.get_generics():
            values = NO_VALUES
            for generic in generic_set:
                if isinstance(generic, (_AbstractAnnotatedClass, TypeVar)):
                    result = generic.define_generics(type_var_dict)
                    values |= result
                    if result != ValueSet({generic}):
                        changed = True
                else:
                    values |= ValueSet([generic])
            new_generics.append(values)

        if not changed:
            # There might not be any type vars that change. In that case just
            # return itself, because it does not make sense to potentially lose
            # cached results.
            return ValueSet([self])

        return ValueSet([self._get_fixed_generics_cls()(
            self._wrapped_value,
            generics=tuple(new_generics)
        )])

    def is_same_class(self, other):
        if not isinstance(other, DefineGenericBase):
            return False

        if self.tree_node != other.tree_node:
            # TODO not sure if this is nice.
            return False
        given_params1 = self.get_generics()
        given_params2 = other.get_generics()

        if len(given_params1) != len(given_params2):
            # If the amount of type vars doesn't match, the class doesn't
            # match.
            return False

        # Now compare generics
        return all(
            any(
                # TODO why is this ordering the correct one?
                cls2.is_same_class(cls1)
                for cls1 in class_set1
                for cls2 in class_set2
            ) for class_set1, class_set2 in zip(given_params1, given_params2)
        )

    def __repr__(self):
        return '<%s: %s%s>' % (
            self.__class__.__name__,
            self._wrapped_value,
            list(self.get_generics()),
        )


class _AbstractAnnotatedClass(ClassMixin, DefineGenericBase):
    def __init__(self, wrapped_class):
        self._wrapped_class = wrapped_class

    def _get_wrapped_value(self):
        return self._wrapped_class

    def get_type_var_filter(self):
        return TypeVarFilter(self.get_generics(), self.list_type_vars())

    def py__call__(self, arguments):
        instance, = super(_AbstractAnnotatedClass, self).py__call__(arguments)
        return ValueSet([InstanceWrapper(instance)])

    def _as_context(self):
        return AnnotatedClassContext(self)

    @to_list
    def py__bases__(self):
        for base in self._wrapped_value.py__bases__():
            yield LazyAnnotatedBaseClass(self, base)


class LazyGenericClass(_AbstractAnnotatedClass):
    def __init__(self, class_value, index_value, value_of_index):
        super(LazyGenericClass, self).__init__(class_value)
        self._index_value = index_value
        self._context_of_index = value_of_index

    @inference_state_method_cache()
    def get_generics(self):
        return list(iter_over_arguments(self._index_value, self._context_of_index))


class GenericClass(_AbstractAnnotatedClass):
    def __init__(self, class_value, generics):
        super(GenericClass, self).__init__(class_value)
        self._generics = generics

    def get_generics(self):
        return self._generics


class LazyAnnotatedBaseClass(object):
    def __init__(self, class_value, lazy_base_class):
        self._class_value = class_value
        self._lazy_base_class = lazy_base_class

    @iterator_to_value_set
    def infer(self):
        for base in self._lazy_base_class.infer():
            if isinstance(base, _AbstractAnnotatedClass):
                # Here we have to recalculate the given types.
                yield GenericClass.create_cached(
                    base.inference_state,
                    base._wrapped_value,
                    tuple(self._remap_type_vars(base)),
                )
            else:
                yield base

    def _remap_type_vars(self, base):
        from jedi.inference.gradual.type_var import TypeVar
        filter = self._class_value.get_type_var_filter()
        for type_var_set in base.get_generics():
            new = NO_VALUES
            for type_var in type_var_set:
                if isinstance(type_var, TypeVar):
                    names = filter.get(type_var.py__name__())
                    new |= ValueSet.from_sets(
                        name.infer() for name in names
                    )
                else:
                    # Mostly will be type vars, except if in some cases
                    # a concrete type will already be there. In that
                    # case just add it to the value set.
                    new |= ValueSet([type_var])
            yield new


class InstanceWrapper(ValueWrapper):
    def py__stop_iteration_returns(self):
        for cls in self._wrapped_value.class_value.py__mro__():
            if cls.py__name__() == 'Generator':
                generics = cls.get_generics()
                try:
                    return generics[2].execute_annotation()
                except IndexError:
                    pass
            elif cls.py__name__() == 'Iterator':
                return ValueSet([builtin_from_name(self.inference_state, u'None')])
        return self._wrapped_value.py__stop_iteration_returns()

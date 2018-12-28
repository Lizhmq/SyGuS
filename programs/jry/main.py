import sys
import sexp
import pprint
import random
import translator
from possibleterm import *
from task import SynthTask
from z3 import *

string2pythonOperator = {
    "+": lambda x, y: x + y,
    "-": lambda x, y: x - y,
    "*": lambda x, y: x * y,
    "div": lambda x, y: x // y,
    "mod": lambda x, y: x % y,
    "ite": lambda b, x, y: x if b else y,
    "=": lambda x, y: x == y,
    "<=": lambda x, y: x <= y,
    ">=": lambda x, y: x >= y,
    "<": lambda x, y: x < y,
    ">": lambda x, y: x > y
}

def stripComments(bmFile):
    noComments = '('
    for line in bmFile:
        line = line.split(';', 1)[0]
        noComments += line
    return noComments + ')'


def getCodeFromTermInfo(TermInfo):
    condition, value = TermInfo[0]
    if len(TermInfo) == 1 or len(condition) == 0:
        return value
    return ["ite", condition, value, getCodeFromTermInfo(TermInfo[1:])]


def getCode(TermInfo, SynFunExpr):
    code = getCodeFromTermInfo(TermInfo)
    FuncDefineStr = '(define-fun'
    for i in range(1, 4):
        currentStr = translator.toString(SynFunExpr[i])
        if i == 2 and len(SynFunExpr[i]) == 1:
            currentStr = "(%s)" % (currentStr)
        FuncDefineStr += " " + currentStr
    FuncDefineStr += ")"
    fullResultCode = FuncDefineStr[:-1] + ' ' + translator.toString(code) + FuncDefineStr[-1]
    return fullResultCode


class BoolTable:
    def __init__(self, VarTable, VarList, Values, Operators):
        self.VarTable = VarTable
        self.Cons = []
        self.TreeTable = []
        self.Root = -1
        self.VarList = VarList
        self.Values = Values
        self.Operators = Operators

    def parseVar(self, var, sample):
        if type(var) == str:
            if var in self.VarTable:
                result = sample[self.VarTable[var]]
                if result is None:
                    if is_int(self.VarTable[var]):
                        return 0
                    else:
                        return True
                if is_int(result):
                    return result.as_long()
                else:
                    return is_true(result)
            return int(var)
        if len(var) == 3:
            return string2pythonOperator[var[0]](self.parseVar(var[1], sample), self.parseVar(var[2], sample))
        else:
            return string2pythonOperator[var[0]](self.parseVar(var[1], sample), self.parseVar(var[2], sample),
                                                 self.parseVar(var[3], sample))

    def getValue(self, var, sample):
        return self.parseVar(var, sample)

    def checkEq(self, var1, var2):
        solver = Solver()
        spec = "(assert (xor %s %s))"%(translator.toString(var1), translator.toString(var2))
        solver.add(parse_smt2_string(spec, decls=self.VarTable))
        result = solver.check()
        if result == sat:
            return [False, solver.model()]
        else:
            return [True, None]

    def insert(self, var, depth):
        if self.Root == -1:
            self.Root = 0
            self.TreeTable.append(var)
            self.Cons[depth].append(var)
            return
        currentNode = self.Root
        while type(self.TreeTable[currentNode][0]) != str:
            sample, lNode, rNode = self.TreeTable[currentNode]
            if self.getValue(var, sample):
                currentNode = lNode
            else:
                currentNode = rNode
        result, newSample = self.checkEq(var, self.TreeTable[currentNode])
        # print(result, newSample)
        if result: return
        lId = len(self.TreeTable)
        rId = len(self.TreeTable) + 1
        if self.getValue(var, newSample):
            self.TreeTable.append(var)
            self.TreeTable.append(self.TreeTable[currentNode])
        else:
            self.TreeTable.append(self.TreeTable[currentNode])
            self.TreeTable.append(var)
        self.TreeTable[currentNode] = [newSample, lId, rId]
        self.Cons[depth].append(var)

    def extendCons(self):
        depth = len(self.Cons)
        self.Cons.append([])
        for operatorType in self.Operators:
            isAllInt = True
            for argType in operatorType[2]:
                if argType != ['Int']:
                    isAllInt = False
            if (not isAllInt) or operatorType[1] != 'Bool':
                continue
            for operator in operatorType[0]:
                for lsize in range(depth + 1):
                    rsize = depth - lsize
                    for lTerm in self.Values.get(lsize):
                        for rTerm in self.Values.get(rsize):
                            # print("tryInsert", [operator, lTerm, rTerm])
                            ConsTable.insert([operator, lTerm, rTerm], depth)

    def getCons(self, depth):
        while len(self.Cons) <= depth:
            self.extendCons()
        return self.Cons[depth]

    def filter(self, depth, example):
        result = []
        for i in range(depth+1):
            for cons in self.getCons(i):
                if self.getValue(cons, example):
                    result.append(cons)
        return result


def reformatListCons(Cons):
    if len(Cons) == 0:
        return []
    result = [Cons[0]]
    for cons in Cons[1:]:
        result = ["and", result, cons]
    return result


def replaceArgs(Term, argList, functionArg):
    newTerm = Term
    for i in range(len(argList)):
        newTerm = replaceTerm(newTerm, argList[i][0], functionArg[i])
    return newTerm


def checkValid(solver, newCons, VarTable, argList, functionArg):
    solver.push()
    if len(newCons) > 0:
        spec = "(assert %s)" % (translator.toString(reformatListCons(replaceArgs(newCons, argList, functionArg))))
        # print(spec)
        solver.add(parse_smt2_string(spec, decls=VarTable))
    result = solver.check()
    # print(result)
    solver.pop()
    if result == unsat:
        return True
    return False


def reduceCons(solver, currentCons, Super, VarTable, argList, functionArg):
    if checkValid(solver, Super, VarTable, argList, functionArg):
        return []
    if len(currentCons) == 1:
        return currentCons
    middle = len(currentCons) // 2
    leftCons = currentCons[: middle]
    rightCons = currentCons[middle:]
    leftRes = reduceCons(solver, leftCons, Super + rightCons, VarTable, argList, functionArg)
    return leftRes + reduceCons(solver, rightCons, Super + leftRes, VarTable, argList, functionArg)


def getTermCondition(Expr, TermInfo, currentTerm, RemainTerms, ConsTable, VarTable):
    SynFunExpr, VarTable, FunDefMap, Constraints = translator.ReadQuery(Expr)

    functionCallDic = {}
    ReplacedConsInfo = []
    for i in range(len(Constraints)):
        ReplacedConsInfo.append(
            replaceFunctionCall(Constraints[i], functionCallDic, SynFunExpr[1], SynFunExpr[3], VarTable))
    ReplacedConsSet = getConsSet(ReplacedConsInfo)
    assert len(ReplacedConsSet) == 1 and len(ReplacedConsSet[0][0]) == 1
    print(VarTable)

    ReplacedCons = ReplacedConsSet[0][1]
    # print(functionCallDic)
    functionCallVar = None
    functionArgs = None
    for functionCallId in functionCallDic:
        functionCallVar, functionArgs = functionCallDic[functionCallId]
    # print(functionCallVar, functionArgs)

    exampleGenerator = Solver()
    checkSolver = Solver()
    for condition, term in TermInfo:
        spec = "(assert (not %s))"%(translator.toString(replaceArgs(condition, SynFunExpr[2], functionArgs)))
        spec = parse_smt2_string(spec, decls=VarTable)
        exampleGenerator.add(spec)
        checkSolver.add(spec)
    for term in RemainTerms:
        spec = "(assert (not (= %s %s)))"%(str(functionCallVar), replaceArgs(term, SynFunExpr[2], functionArgs))
        exampleGenerator.add(parse_smt2_string(spec, decls=VarTable))
    spec = "(assert (= %s %s))"%(str(functionCallVar), replaceArgs(currentTerm, SynFunExpr[2], functionArgs))
    spec = parse_smt2_string(spec, decls=VarTable)
    exampleGenerator.add(spec)
    checkSolver.add(spec)
    spec = "\n".join(list(map(lambda x: "(assert %s)" % (translator.toString(x[1:])), ReplacedCons)))
    spec = parse_smt2_string(spec, decls=VarTable)
    exampleGenerator.add(spec)
    checkSolver.add(Not(And(spec)))
    # print(checkSolver)

    depth = 0
    result = []
    while True:
        exampleResult = exampleGenerator.check()
        if exampleResult == unsat:
            break
        example = exampleGenerator.model()
        suitableCons = ConsTable.filter(depth, example)
        # print(example)
        if not checkValid(checkSolver, suitableCons, VarTable, SynFunExpr[2], functionArgs):
            depth += 1
            continue
        reducedCondition = reduceCons(checkSolver, suitableCons, [], VarTable, SynFunExpr[2], functionArgs)
        reducedCondition = reformatListCons(reducedCondition)
        # print(reducedCondition)
        # input()
        if len(reducedCondition) > 0:
            if len(result) == 0:
                result = reducedCondition
            else:
                result = ["or", result, reducedCondition]
            spec = "(assert (not %s))"%(translator.toString(replaceArgs(reducedCondition, SynFunExpr[2], functionArgs)))
            exampleGenerator.add(parse_smt2_string(spec, decls=VarTable))
        else:
            return []

    return result


if __name__ == '__main__':
    task = SynthTask(sys.argv[1])
    bmExpr = task.ins.bmExpr
    print(bmExpr)
    #print (checker.check('(define-fun f ((x Int)) Int (mod (* x 3) 10)  )'))
    #raw_input()
    SynFunExpr = []
    StartSym = 'My-Start-Symbol' #virtual starting symbol
    for expr in bmExpr:
        if len(expr)==0:
            continue
        elif expr[0]=='synth-fun':
            SynFunExpr=expr
    FuncDefine = ['define-fun']+SynFunExpr[1:4] #copy function signature
    Productions = {StartSym:[]}
    ReturnType = SynFunExpr[3]
    Type = {StartSym:SynFunExpr[3]} # set starting symbol's return type
    Terminals = {'Int':[], 'Bool':[]}
    Operators = []

    for NonTerm in SynFunExpr[4]: #SynFunExpr[4] is the production rule
        NTName = NonTerm[0]
        NTType = NonTerm[1]
        assert NTType in ['Int', 'Bool']
        if NTType == Type[StartSym]:
            Productions[StartSym].append(NTName)
        Type[NTName] = NTType
        #Productions[NTName] = NonTerm[2]
        Productions[NTName] = []
        for NT in NonTerm[2]:
            if type(NT) == tuple:
                Productions[NTName].append(str(NT[1])) # deal with ('Int',0). You can also utilize type information, but you will suffer from these tuples.
            else:
                Productions[NTName].append(NT)

    SynFunExpr, VarTable, _, Constraints, checker = translator.ReadQuery(bmExpr, True)

    operatorTable = {}
    for NonTerm in SynFunExpr[4]:
        for NT in NonTerm[2]:
            current = NT
            if type(NT) == tuple:
                current = str(NT[1])
            if type(current) == str:
                if current not in Type and current not in Terminals[NonTerm[1]]:
                    Terminals[NonTerm[1]].append(current)
            else:
                operatorArgs = []
                for i in NT[1:]:
                    if i in Type:
                        operatorArgs.append([Type[i]])
                    else:
                        operatorArgs.append(i)
                operatorStr = str([NonTerm[1], operatorArgs])
                if operatorStr in operatorTable:
                    operatorLoc = operatorTable[operatorStr]
                    Operators[operatorLoc][0].append(NT[0])
                else:
                    operator = [[NT[0]], NonTerm[1]]
                    operator.append(operatorArgs)
                    operatorTable[operatorStr] = len(Operators)
                    Operators.append(operator)
    Operators = simplifyOperator(Operators)
    # print(Terminals)
    #print(Operators)

    PossibleTerm, Values = getPossibleValue(Operators, bmExpr, Terminals)
    if len(PossibleTerm) == 1:
        print(getCode([[[], PossibleTerm[0]]], SynFunExpr))
        exit(0)

    argVarTable = {}
    for arg in SynFunExpr[2]:
        declareVar(arg[1], arg[0], argVarTable)
    argVarTable["__result"] = Bool("__result")

    TermInfo = []
    ConsTable = BoolTable(argVarTable, SynFunExpr[2], Values, Operators)
    for i in range(len(PossibleTerm)):
        term = PossibleTerm[i]
        TermInfo.append([getTermCondition(bmExpr, TermInfo, term, PossibleTerm[i+1:], ConsTable, VarTable), term])

    resultCode = getCode(TermInfo, SynFunExpr)
    print(resultCode)
    print(checker.check(resultCode))